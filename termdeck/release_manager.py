"""Deterministic release construction and atomic managed installation for T1000.

The release manager is intentionally local and offline.  It does not update
itself, contact a registry, or stop a terminal process.  Its mutation model is:

1. verify a content-addressed wheel and canonical manifest;
2. stage and verify an immutable release directory on the destination volume;
3. publish one small ``state.json`` pointer under an exclusive advisory lock;
4. keep the old release as the rollback target.

The stable launcher reads only ``state.json``.  A failed build or install cannot
partially rewrite the currently selected release, and the already-running GUI
continues using its loaded modules until a separate, explicit activation step.
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import csv
import errno
import fcntl
import hashlib
import io
import json
import os
import pwd
import re
import selectors
import shlex
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 keeps TOML in the compatible backport.
    import tomli as tomllib
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from email.parser import Parser
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, NoReturn, Sequence

from .build_info import (
    BUILD_INFO_SCHEMA,
    MANAGED_STATE_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    BuildIdentityError,
    canonical_json_bytes,
    compute_build_id,
    compute_release_id,
    default_managed_root,
    read_json,
    sha256_bytes,
    sha256_file,
    validate_build_info,
    validate_managed_state as validate_managed_state_identity,
    validate_release_manifest,
)
from .models import SessionRecord
from .session_store import DEFAULT_DB_PATH
from .tmux_backend import TmuxBackend, TmuxError

_MAX_WHEEL_ENTRIES = 20_000
_MAX_WHEEL_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_WHEEL_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_WHEEL_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_WHEEL_PATH_COMPONENTS = 256
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_SITE_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_RELEASE_VERIFICATION_BYTES = _MAX_SITE_MANIFEST_BYTES + 64 * 1024
_MAX_SITE_DIRECTORIES = 100_000
_MAX_ACTIVATION_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_ACTIVATION_PROBE_OUTPUT_BYTES = 6 * 1024 * 1024
_MAX_ACTIVATION_PROBE_STDERR_BYTES = 64 * 1024
_ACTIVATION_PROBE_SCHEMA = "t1000.activation-probe.v1"
_RELEASE_VERIFICATION_SCHEMA = "t1000.release-verification.v2"
_SITE_MANIFEST_SCHEMA = "t1000.installed-site-manifest.v1"
_AUTOSTART_RECEIPT_SCHEMA = "t1000.autostart-activation-receipt.v1"
_AUTOSTART_DESCRIPTOR_NAME = "io.github.t1000.T1000.desktop"
_AUTOSTART_RECEIPT_NAME = "autostart-receipt.json"
_MAX_AUTOSTART_DESCRIPTOR_BYTES = 64 * 1024
_MAX_AUTOSTART_RECEIPT_BYTES = 64 * 1024
_SERVICE_WRAPPER_PREIMAGE_SCHEMA = "t1000.service-wrapper-preimage.v1"
_SERVICE_WRAPPER_PREIMAGE_NAME = "service-wrapper-preimage.json"
_FIRST_INSTALL_PREIMAGE_SCHEMA = "t1000.first-install-preimage.v1"
_FIRST_INSTALL_PREIMAGE_NAME = "first-install-preimage.json"
_MAX_FIRST_INSTALL_PREIMAGE_BYTES = 64 * 1024
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_PUBLICATION_EINTR_RETRIES = 8
_PUBLICATION_JOURNAL_SCHEMA = "t1000.publication-transaction.v1"
_PUBLICATION_JOURNAL_PREFIX = ".termdeck-publication-"
_PUBLICATION_JOURNAL_SUFFIX = ".json"
# Each append-only frame contains the complete authenticated preimage set, so
# a multi-wrapper publication legitimately accumulates several frames.  Bound
# a single frame tightly enough to reject a malicious giant record, while
# allowing the complete durable log to hold its ordered recovery history.
_MAX_PUBLICATION_JOURNAL_FRAME_BYTES = 8 * 1024 * 1024
_MAX_PUBLICATION_JOURNAL_BYTES = 64 * 1024 * 1024
# A release install and its inverse must never reuse a fixed journal pathname.
# Activation keeps the exact journal inode in its outer durable receipt.  If an
# ordinary install cleaned its terminal journal and a later B-to-A rollback
# created a new inode at the same name, a crash during that inverse would make
# the old receipt look like a hostile replacement instead of the independent
# rollback transaction it really is.
_INSTALL_PUBLICATION_JOURNAL_NAME = ".termdeck-publication-install.json"
_ROLLBACK_PUBLICATION_JOURNAL_NAME = ".termdeck-publication-rollback.json"
_RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION_PREFIX = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:[A-Za-z0-9.+-].*)?$"
)
_FROZEN_RELEASE_MIN_VERSION = (0, 2, 13)
_CANONICAL_WHEEL_METADATA_MIN_VERSION = (0, 2, 14)
# Current managed releases carry the service-wrapper and activation modules
# inside their frozen wheel site.  Older rollback fixtures legitimately lack
# those modules, so they retain their historical source-root wrappers.
_IMMUTABLE_WRAPPER_EXECUTION_MIN_VERSION = (0, 2, 21)
_ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP_CENTRAL_HEADER_BYTES = 46
_ZIP_EOCD_BYTES = 22
_ZIP_MAX_COMMENT_BYTES = 65_535
_BUILD_RUNNER_SOURCE = (
    "import os, runpy\n"
    "os.umask(0o22)\n"
    "runpy.run_module('build', run_name='__main__', alter_sys=True)\n"
)
_MANAGED_BIN_DIR_ENV = "T1000_MANAGED_BIN_DIR"
# The MaxCloud managed root is deliberately bound to the already-populated
# physical TermDeck state directory. This keeps the generated CLI launcher,
# web service, and auth-scope defaults on one database without copying or
# merging an alternate registry.
_DEPLOYMENT_MANAGED_ROOT = Path(
    "/warp/runtime/termdeck/t1000-managed"
)
_DEPLOYMENT_STATE_ROOT = Path("/warp/state/termdeck")
# Root owns the attestation endpoint; managed wrappers carry its canonical
# path but never the HMAC/signing material.  Only the distinct
# ``termdeck-preparer`` group can traverse its 0710 namespace; ``max`` is
# explicitly excluded.  A runtime file owned by ``max`` cannot be a producer
# authority because every same-UID service may replace it, regardless of a
# 0600 mode.
_CANONICAL_CONTROL_ROOT = Path("/etc/termdeck/control")
_CONTROL_ATTEST_SOCKET_NAME = "runtime-attestd.sock"
_CONTROL_ATTEST_OPERATION = "sign"
_CONTROL_ATTEST_REQUEST_SCHEMA = "termdeck.lan-runtime-build.v2"
_CONTROL_ATTEST_RESPONSE_SCHEMA = "termdeck.runtime-attestation.v1"
_CONTROL_ATTEST_PRODUCER = "termdeck-preparer"
_CONTROL_ATTEST_OWNER = "root"
_CONTROL_ATTEST_GROUP = _CONTROL_ATTEST_PRODUCER
_CONTROL_ATTEST_EXCLUDED_PRINCIPAL = "max"
_CONTROL_ROOT_MODE = 0o710
_CONTROL_ATTEST_SOCKET_MODE = 0o660
# The source checkout is the deployment's canonical release identity. The
# managed release files remain under _DEPLOYMENT_MANAGED_ROOT.
_CANONICAL_SOURCE_ROOT = Path("/warp/projects/software/termdeck")
_UUID = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_MANAGED_GUI_PROCESS_SCHEMA = "t1000.managed-gui-processes.v1"
_PROCESS_OBSERVER_RECEIPT_SCHEMA = "t1000.managed-gui-process-observer-receipt.v1"
_PROCESS_OBSERVER_ENV = "T1000_PROCESS_SCAN_BROKER"
_PROCESS_OBSERVER_TIMEOUT_SECONDS = 10.0
_PROCESS_OBSERVER_STDOUT_BYTES = 2 * 1024 * 1024
_PROCESS_OBSERVER_STDERR_BYTES = 64 * 1024
_PROCESS_OBSERVER_FOLLOWER_WAIT_SECONDS = _PROCESS_OBSERVER_TIMEOUT_SECONDS + 5.0
_PROCESS_DIRECT_SCAN_FOLLOWER_WAIT_SECONDS = 5.0
_PROCESS_CROSS_STATE_SCHEMA = "t1000.process-observer-cross-flight.v2"
_PROCESS_CROSS_RESULT_SCHEMA = "t1000.process-observer-cross-result.v1"
_PROCESS_CROSS_ROOT_NAME = "t1000-process-observer"
_PROCESS_CROSS_REGISTRY_LOCK_NAME = "registry.lock"
_PROCESS_CROSS_LOCK_WAIT_SECONDS = 2.0
_PROCESS_CROSS_LOCK_MAX_WAIT_SECONDS = 10.0
_PROCESS_CROSS_LOCK_POLL_SECONDS = 0.01
_PROCESS_CROSS_LEGACY_LOCK_NAME = re.compile(r"^flight-([0-9a-f]{64})\.lock$")
_PROCESS_CROSS_WAIT_SECONDS = _PROCESS_OBSERVER_TIMEOUT_SECONDS + 5.0
_PROCESS_CROSS_MAX_AGE_NS = int((_PROCESS_CROSS_WAIT_SECONDS + 5.0) * 1_000_000_000)
_PROCESS_CROSS_LATE_JOIN_TTL_NS = _PROCESS_CROSS_MAX_AGE_NS
_PROCESS_CROSS_RESULT_TTL_NS = 60 * 1_000_000_000
_PROCESS_CROSS_MAX_STATE_BYTES = 16 * 1024
_PROCESS_CROSS_MAX_RESULT_BYTES = 4 * 1024 * 1024
_PROCESS_CROSS_MAX_CLEANUP_ENTRIES = 256
_PROCESS_CROSS_MAX_DIRECTORY_ENTRIES = 8192
_PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES = 64 * 1024 * 1024
_PROCESS_CROSS_RESULT_NAME = re.compile(
    r"^result-([0-9a-f]{64})-([0-9a-f]{32})\.json$"
)
_PROCESS_CROSS_STATE_NAME = re.compile(r"^flight-([0-9a-f]{64})\.json$")
_PROCESS_CROSS_FILE_STATE = "state"
_PROCESS_CROSS_FILE_RESULT = "result"
_PROCESS_OBSERVER_STABLE_FIELDS = (
    "schema",
    "managed_release_configured",
    "expected_uid",
    "boot_id",
    "expected_identity",
    "complete",
    "posture",
    "gui_count",
    "processes",
    "activation",
    "issues",
)
_PROCESS_OBSERVER_HELPER_SOURCE = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path

SCHEMA = "t1000.managed-gui-process-observer-receipt.v1"

try:
    source_root = Path(sys.argv[1]).resolve(strict=True)
    expected_module = Path(sys.argv[2]).resolve(strict=True)
    managed_root = Path(sys.argv[3]).absolute()
    managed_bin = Path(sys.argv[4]).absolute()
    expected_uid = int(sys.argv[5])
    sys.path.insert(0, str(source_root))
    from termdeck import release_manager as manager

    observed_module = Path(manager.__file__).resolve(strict=True)
    if observed_module != expected_module:
        raise RuntimeError("observer module path mismatch")
    payload = manager.managed_gui_process_observer_receipt(
        root=managed_root,
        bin_dir=managed_bin,
        expected_uid=expected_uid,
    )
except Exception:
    payload = {"schema": SCHEMA, "ok": False, "error_code": "observer-failed"}

sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
raise SystemExit(0 if payload.get("ok") else 3)
'''
_BOOT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ReleaseError(RuntimeError):
    """A release failed a safety, identity, or atomicity contract."""


def _require_production_build(
    build_info: Mapping[str, object],
    *,
    label: str,
) -> None:
    if build_info.get("build_mode") != "release":
        raise ReleaseError(
            f"{label} is not a production release "
            f"(build mode={build_info.get('build_mode')!r})"
        )


def _require_managed_storage(layout: "ReleaseLayout"):
    from .storage_fabric import require_hot_storage

    try:
        return require_hot_storage(
            managed_root=layout.root,
            launcher_path=layout.launcher_path,
            production=True,
        )
    except Exception as exc:
        reason = getattr(exc, "reason", str(exc))
        raise ReleaseError(f"managed storage attestation failed: {reason}") from exc


def _storage_attestation_binding(attestation: object) -> str:
    try:
        payload = attestation.as_dict()  # type: ignore[attr-defined]
    except Exception as exc:
        raise ReleaseError(f"managed storage attestation is not serializable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ReleaseError("managed storage attestation is not an object")
    raw_rows = payload.get("path_evidence")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        paths = payload.get("paths")
        raw_rows = list(paths.values()) if isinstance(paths, Mapping) else []
    evidence_fields = (
        "name",
        "requested",
        "resolved",
        "role",
        "device",
        "mount_point",
        "source",
        "filesystem",
        "rotational",
        "storage_class",
    )

    def normalized_evidence(value: Mapping[object, object]) -> dict[str, object]:
        return {key: value.get(key) for key in evidence_fields}

    rows: list[dict[str, object]] = []
    for value in raw_rows:
        if not isinstance(value, Mapping):
            raise ReleaseError("managed storage attestation path evidence is invalid")
        rows.append(normalized_evidence(value))
    rows.sort(key=lambda row: str(row.get("name", "")))
    launcher_value = payload.get("launcher")
    launcher = (
        normalized_evidence(launcher_value)
        if isinstance(launcher_value, Mapping)
        else None
    )
    environment_value = payload.get("environment")
    environment = None
    if isinstance(environment_value, Mapping):
        environment = {
            key: environment_value.get(key)
            for key in (
                "expected",
                "observed",
                "matches",
                "status",
                "advisory",
                "used_for_admission",
            )
        }
    binding = {
        "schema": payload.get("schema"),
        "fabric": payload.get("fabric", payload.get("storage_fabric")),
        "storage_class": payload.get("storage_class"),
        "production": payload.get("production"),
        "paths": rows,
        "launcher": launcher,
        "environment": environment,
    }
    return sha256_bytes(canonical_json_bytes(binding))


def _revalidate_managed_storage(
    layout: "ReleaseLayout",
    expected_binding: str,
    *,
    phase: str,
):
    observed = _require_managed_storage(layout)
    if _storage_attestation_binding(observed) != expected_binding:
        raise ReleaseError(f"managed storage identity changed {phase}")
    return observed


@dataclass(frozen=True)
class SourceSnapshot:
    root: Path
    source_commit: str
    source_date_epoch: int
    source_tree_sha256: str
    build_mode: str
    version: str
    files: tuple[str, ...]
    dirty_entries: tuple[str, ...]

    def build_info(self) -> dict[str, object]:
        info: dict[str, object] = {
            "schema": BUILD_INFO_SCHEMA,
            "version": self.version,
            "source_commit": self.source_commit,
            "source_tree_sha256": self.source_tree_sha256,
            "source_date_epoch": self.source_date_epoch,
            "build_mode": self.build_mode,
        }
        info["build_id"] = compute_build_id(info)
        return validate_build_info(info)


@dataclass(frozen=True)
class VerifiedWheel:
    path: Path
    sha256: str
    size: int
    distribution: str
    version: str
    dist_info: str
    build_info: dict[str, object]
    record_rows: tuple[tuple[str, str, str], ...]
    members: tuple[str, ...]
    manifest: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "wheel": self.path.name,
            "wheel_path": str(self.path),
            "wheel_sha256": self.sha256,
            "wheel_size": self.size,
            "distribution": self.distribution,
            "version": self.version,
            "dist_info": self.dist_info,
            "build_info": self.build_info,
            "member_count": len(self.members),
            "release_id": self.manifest.get("release_id") if self.manifest else None,
        }


@dataclass(frozen=True)
class ReleaseLayout:
    root: Path
    releases_dir: Path
    state_path: Path
    lock_path: Path
    bootstrap_path: Path
    bin_dir: Path
    launcher_path: Path
    legacy_dir: Path


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    content: bytes = b""
    mode: int = 0


@dataclass(frozen=True)
class _ActivationBundle:
    """Canonical bootstrap and launcher bytes for one managed release generation."""

    bootstrap: bytes
    launcher: bytes
    bootstrap_sha256: str
    launcher_sha256: str
    origin: str


@dataclass(frozen=True)
class _AutostartPublication:
    descriptor_path: Path
    receipt_path: Path
    descriptor: bytes
    receipt: bytes
    descriptor_preimage: _FileSnapshot
    receipt_preimage: _FileSnapshot
    repaired: bool


@dataclass(frozen=True)
class _ServiceWrapperPublication:
    """One wrapper publication and the bytes needed to undo it.

    ``service_wrappers`` owns rendering and destination validation.  The
    release manager keeps the preimage here so a later activation failure can
    put the complete wrapper set back before returning the error.
    """

    api: object
    kwargs: dict[str, object]
    target_release_id: str | None
    preimage: object
    legacy_preimage_path: Path
    legacy_preimage_before: "_AnchoredFileSnapshot"
    legacy_preimage_write: bytes | None
    expected_paths: tuple[Path, ...]
    parent_guards: object
    plan: object
    repaired: bool


@dataclass(frozen=True)
class _FirstInstallPreimagePublication:
    """Durable proof that an initially empty activation can be undone safely."""

    receipt_path: Path
    receipt_preimage: _FileSnapshot
    activation_preimages: dict[Path, _FileSnapshot]
    parent_guards: object


@dataclass(frozen=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _ProcessObserverTransportResult:
    """Immutable output shared only while one broker transport is in progress."""

    result_code: str
    stdout: bytes = b""
    cross_process_role: str = "none"
    cross_process_shared: bool = False
    cross_process_result: str = "not-attempted"


@dataclass(frozen=True)
class _ProcessCrossAdmission:
    role: str
    root: Path
    key_sha256: str
    nonce: str
    result_path: Path
    state_path: Path
    lock_path: Path
    boot_id: str
    leader_pid: int
    leader_start_ticks: int


@dataclass(frozen=True)
class _ProcessCrossUsage:
    """Bounded protocol storage measured from one directory snapshot."""

    directory_entries: int
    result_count: int
    state_count: int
    total_result_bytes: int


class _ProcessObserverFlight:
    """One process-local in-progress observation; never a completed-result cache."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: _ProcessObserverTransportResult | None = None


_PROCESS_OBSERVER_FLIGHT_LOCK = threading.Lock()
_PROCESS_OBSERVER_FLIGHTS: dict[tuple[str, ...], _ProcessObserverFlight] = {}


@dataclass(frozen=True)
class _ActivationArtifactSnapshot:
    manifest: dict[str, object]
    manifest_bytes: bytes
    wheel_name: str
    wheel_bytes: bytes


def _default_managed_bin_dir(
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get(_MANAGED_BIN_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "bin"


def release_layout(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
) -> ReleaseLayout:
    managed = (
        Path(root).expanduser() if root is not None else _default_release_root()
    ).absolute()
    binaries = (
        Path(bin_dir).expanduser() if bin_dir is not None else _default_managed_bin_dir()
    ).absolute()
    return ReleaseLayout(
        root=managed,
        releases_dir=managed / "releases",
        state_path=managed / "state.json",
        lock_path=managed / "state.lock",
        bootstrap_path=managed / "bootstrap.py",
        bin_dir=binaries,
        launcher_path=binaries / "t1000",
        legacy_dir=managed / "legacy",
    )


def _canonical_source_root() -> Path:
    """Return the immutable source/build identity for the MaxCloud checkout."""

    return _CANONICAL_SOURCE_ROOT.resolve(strict=False)


def _canonical_managed_root() -> Path:
    """Return the mutable managed-release root for the MaxCloud deployment."""

    return _DEPLOYMENT_MANAGED_ROOT.resolve(strict=False)


def _default_release_root(*, env: Mapping[str, str] | None = None) -> Path:
    return (
        Path(default_managed_root(env=os.environ if env is None else env))
        .expanduser()
        .absolute()
    )


def _is_canonical_production_root(path: Path) -> bool:
    canonical = _canonical_managed_root()
    try:
        if path.resolve(strict=False) == canonical.resolve(strict=False):
            return True
    except OSError:
        pass
    try:
        return path.exists() and canonical.exists() and path.samefile(canonical)
    except OSError:
        return False


def _deployment_state_root(layout: ReleaseLayout) -> Path | None:
    """Return the validated physical state root for the MaxCloud deployment."""

    try:
        if layout.root.resolve(strict=False) == _DEPLOYMENT_MANAGED_ROOT.resolve(
            strict=False
        ):
            return _DEPLOYMENT_STATE_ROOT
    except OSError:
        pass
    return None


def _resolve_production_intent(
    layout: ReleaseLayout,
    production: bool | None,
) -> bool:
    """Prevent callers from downgrading the canonical production root."""

    if production is not None and not isinstance(production, bool):
        raise ReleaseError("production intent must be a boolean when provided")
    if _is_canonical_production_root(layout.root):
        return True
    return bool(production)


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"command failed to execute: {shlex.join(argv)}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise ReleaseError(
            f"command exited {completed.returncode}: {shlex.join(argv)}: {detail}"
        )
    return completed


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Boundedly terminate a child and every descendant in its private group.

    The group leader may already have exited while a descendant still owns an
    inherited pipe. Group existence, not the leader's return code, therefore
    controls escalation.
    """

    process_group = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    if group_exists():
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 0.5
        while group_exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if group_exists():
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 0.5
            while group_exists() and time.monotonic() < deadline:
                time.sleep(0.01)
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_bounded_output(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> _BoundedProcessResult:
    """Run one process with hard in-memory output and wall-clock bounds.

    Pipes are drained incrementally through a selector. The child receives a new
    process group so timeout or overflow terminates descendants as well as the
    direct process, preventing a probe from retaining pipes after its parent exits.
    """

    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ReleaseError("bounded command argv is empty or invalid")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ReleaseError("bounded command timeout must be positive")
    for label, limit in (("stdout", stdout_limit), ("stderr", stderr_limit)):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ReleaseError(f"bounded command {label} limit must be positive")
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise ReleaseError(f"command failed to execute: {shlex.join(argv)}: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    deadline = time.monotonic() + float(timeout)
    try:
        for label, stream in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseError(f"command timed out after {timeout:.3f}s: {shlex.join(argv)}")
            events = selector.select(min(0.1, remaining))
            if not events:
                continue
            for key, _mask in events:
                label = str(key.data)
                buffer = buffers[label]
                limit = limits[label]
                try:
                    chunk = os.read(key.fd, min(64 * 1024, limit - len(buffer) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise ReleaseError(
                        f"command {label} exceeded the {limit}-byte limit: {shlex.join(argv)}"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError(f"command timed out after {timeout:.3f}s: {shlex.join(argv)}")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ReleaseError(
                f"command timed out after {timeout:.3f}s: {shlex.join(argv)}"
            ) from exc
        stdout = bytes(buffers["stdout"])
        stderr = bytes(buffers["stderr"])
        if returncode != 0:
            detail_bytes = stderr or stdout or b"command failed"
            detail = detail_bytes[:4096].decode("utf-8", "replace").strip()
            raise ReleaseError(
                f"command exited {returncode}: {shlex.join(argv)}: {detail}"
            )
        return _BoundedProcessResult(returncode, stdout, stderr)
    except Exception:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        for stream in streams.values():
            stream.close()
        if process.poll() is None:
            _terminate_process_group(process)


def _git(root: Path, *args: str, timeout: float = 30.0) -> str:
    return _run(["git", *args], cwd=root, timeout=timeout).stdout


def _git_paths(root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ReleaseError(
            "git ls-files failed: " + completed.stderr.decode("utf-8", "replace").strip()
        )
    paths = tuple(
        sorted(
            item.decode("utf-8", "surrogateescape")
            for item in completed.stdout.split(b"\0")
            if item
        )
    )
    if not paths:
        raise ReleaseError("source snapshot contains no files")
    return paths


def _dirty_entries(root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ReleaseError(
            "git status failed: " + completed.stderr.decode("utf-8", "replace").strip()
        )
    return tuple(
        item.decode("utf-8", "surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _source_version(root: Path) -> str:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        version = str(project["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"could not read project version: {exc}") from exc
    init = (root / "termdeck" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', init, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise ReleaseError("pyproject.toml and termdeck.__version__ disagree")
    bundled_path = root / "termdeck" / "_build_info.json"
    try:
        bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"could not read source build-info metadata: {exc}") from exc
    if isinstance(bundled, Mapping) and "version" in bundled:
        if bundled.get("version") != version:
            raise ReleaseError(
                "pyproject.toml and source termdeck/_build_info.json disagree"
            )
    return version


def _file_mode(path: Path) -> str:
    mode = path.stat(follow_symlinks=False).st_mode
    if stat.S_ISLNK(mode):
        raise ReleaseError(f"source snapshot rejects symbolic link: {path}")
    if not stat.S_ISREG(mode):
        raise ReleaseError(f"source snapshot rejects non-regular file: {path}")
    return "100755" if mode & stat.S_IXUSR else "100644"


def _tree_digest(root: Path, paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        if "\x00" in relative or relative.startswith("/"):
            raise ReleaseError(f"unsafe source path: {relative!r}")
        path = root / relative
        try:
            mode_before = _file_mode(path)
            content = _read_regular_file_bytes(
                path,
                label=f"source file {relative!r}",
                maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
            )
            mode_after = _file_mode(path)
        except OSError as exc:
            raise ReleaseError(f"could not snapshot source file {relative!r}: {exc}") from exc
        if mode_before != mode_after:
            raise ReleaseError(f"source file mode changed while being snapshotted: {relative!r}")
        mode = mode_before
        record = {
            "path": relative,
            "mode": mode,
            "size": len(content),
            "sha256": sha256_bytes(content),
        }
        digest.update(canonical_json_bytes(record))
    return digest.hexdigest()


def snapshot_source(
    root: str | Path,
    *,
    allow_dirty: bool = False,
) -> SourceSnapshot:
    requested = Path(root).expanduser().absolute()
    requested_mode = requested.lstat().st_mode
    if stat.S_ISLNK(requested_mode) or not stat.S_ISDIR(requested_mode):
        raise ReleaseError("source root must be a real directory, not a symlink")
    source = requested.resolve(strict=True)
    if not (source / ".git").exists():
        raise ReleaseError(f"source root is not a Git worktree: {source}")
    dirty = _dirty_entries(source)
    if dirty and not allow_dirty:
        preview = ", ".join(entry.replace("\n", "\\n") for entry in dirty[:10])
        raise ReleaseError(f"release build requires a clean worktree; dirty entries: {preview}")
    commit = _git(source, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ReleaseError("Git HEAD is not a full object id")
    epoch_text = _git(source, "show", "-s", "--format=%ct", "HEAD").strip()
    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise ReleaseError(f"Git commit timestamp is invalid: {epoch_text!r}") from exc
    paths = _git_paths(source)
    tree_hash = _tree_digest(source, paths)
    version = _source_version(source)
    commit_after = _git(source, "rev-parse", "HEAD").strip()
    dirty_after = _dirty_entries(source)
    if commit_after != commit or dirty_after != dirty:
        raise ReleaseError("source worktree changed while its release identity was captured")
    return SourceSnapshot(
        root=source,
        source_commit=commit + ("+dirty" if dirty else ""),
        source_date_epoch=epoch,
        source_tree_sha256=tree_hash,
        build_mode="dirty-candidate" if dirty else "release",
        version=version,
        files=paths,
        dirty_entries=dirty,
    )


def _copy_snapshot(snapshot: SourceSnapshot, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    epoch = snapshot.source_date_epoch
    for relative in snapshot.files:
        source = snapshot.root / relative
        target = destination / relative
        source_mode = _file_mode(source)
        content = _read_regular_file_bytes(
            source,
            label=f"source file {relative!r}",
            maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
        )
        if _file_mode(source) != source_mode:
            raise ReleaseError(f"source file changed mode during export: {relative!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o755 if source_mode == "100755" else 0o644
        _atomic_write_bytes(target, content, mode=mode)
        os.utime(target, (epoch, epoch), follow_symlinks=False)
    exported_digest = _tree_digest(destination, snapshot.files)
    if exported_digest != snapshot.source_tree_sha256:
        raise ReleaseError(
            "source changed after snapshot; exported tree does not match source identity"
        )
    build_info_path = destination / "termdeck" / "_build_info.json"
    build_info_path.write_bytes(canonical_json_bytes(snapshot.build_info()))
    build_info_path.chmod(0o644)
    os.utime(build_info_path, (epoch, epoch), follow_symlinks=False)
    # Directory timestamps can enter source archives and tool-generated metadata.
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        os.utime(directory, (epoch, epoch), follow_symlinks=False)
    os.utime(destination, (epoch, epoch), follow_symlinks=False)


def _wheel_eocd_offset(content: bytes) -> int:
    """Locate one EOCD record that accounts for the exact archive length."""

    if len(content) < _ZIP_EOCD_BYTES:
        raise ReleaseError("built wheel has no complete end of central directory")
    lower = max(0, len(content) - _ZIP_EOCD_BYTES - _ZIP_MAX_COMMENT_BYTES)
    cursor = content.rfind(_ZIP_EOCD_SIGNATURE, lower)
    while cursor >= 0:
        if cursor + _ZIP_EOCD_BYTES <= len(content):
            comment_length = struct.unpack_from("<H", content, cursor + 20)[0]
            if cursor + _ZIP_EOCD_BYTES + comment_length == len(content):
                return cursor
        cursor = content.rfind(_ZIP_EOCD_SIGNATURE, lower, cursor)
    raise ReleaseError("built wheel end of central directory is missing or has trailing bytes")


def _canonical_wheel_external_attr(*, directory: bool) -> int:
    mode = (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)
    return (mode << 16) | (0x10 if directory else 0)


def _wheel_unpatched_identity(info: zipfile.ZipInfo) -> tuple[object, ...]:
    """Fields that central-directory permission normalization must preserve."""

    return (
        info.filename,
        info.CRC,
        info.file_size,
        info.compress_size,
        info.compress_type,
        info.date_time,
        info.header_offset,
        info.flag_bits,
        info.extra,
        info.comment,
        info.extract_version,
        info.create_version,
        info.volume,
    )


def _canonicalize_built_wheel_metadata(wheel_path: str | Path) -> None:
    """Normalize only ZIP central metadata that can inherit caller umask.

    Backend wheel payloads, compression streams, local headers, offsets, CRCs,
    timestamps, ordering, and RECORD content remain byte-for-byte untouched.
    The accepted build grammar is deliberately narrower than the general ZIP
    reader: one disk, no ZIP64 sentinels, no comment/trailing bytes, and one
    exact central-directory sequence.  Any malformed input fails before the
    original temporary wheel is replaced.
    """

    wheel = Path(wheel_path).expanduser().absolute()
    original = _read_regular_file_bytes(
        wheel,
        label="backend-built wheel",
        maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
    )
    eocd = _wheel_eocd_offset(original)
    (
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack_from("<4H2IH", original, eocd + 4)
    if comment_length != 0:
        raise ReleaseError("backend-built wheel must not contain an archive comment")
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        raise ReleaseError("backend-built wheel must use one complete ZIP disk")
    if (
        total_entries in {0, 0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ReleaseError("backend-built wheel uses unsupported empty or ZIP64 metadata")
    if total_entries > _MAX_WHEEL_ENTRIES:
        raise ReleaseError(f"backend-built wheel entry count is unsafe: {total_entries}")
    if central_offset + central_size != eocd:
        raise ReleaseError("backend-built wheel central-directory bounds are inconsistent")

    try:
        archive = zipfile.ZipFile(io.BytesIO(original), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"could not open backend-built wheel: {exc}") from exc
    with archive:
        infos = _validate_wheel_members(archive)
        if len(infos) != total_entries or archive.start_dir != central_offset:
            raise ReleaseError("backend-built wheel central-directory count or offset disagrees")
        if infos[0].header_offset != 0:
            raise ReleaseError("backend-built wheel contains a noncanonical archive prefix")
        before_identity = tuple(_wheel_unpatched_identity(info) for info in infos)

    normalized = bytearray(original)
    cursor = central_offset
    changed_ranges: list[tuple[int, int]] = []
    for info in infos:
        if cursor + _ZIP_CENTRAL_HEADER_BYTES > eocd:
            raise ReleaseError("backend-built wheel has a truncated central header")
        if original[cursor : cursor + 4] != _ZIP_CENTRAL_SIGNATURE:
            raise ReleaseError("backend-built wheel has a malformed central header signature")
        extract_version = struct.unpack_from("<H", original, cursor + 6)[0]
        flags = struct.unpack_from("<H", original, cursor + 8)[0]
        compressed_size, uncompressed_size = struct.unpack_from(
            "<II", original, cursor + 20
        )
        filename_length, extra_length, member_comment_length = struct.unpack_from(
            "<HHH", original, cursor + 28
        )
        disk_start = struct.unpack_from("<H", original, cursor + 34)[0]
        local_header_offset = struct.unpack_from("<I", original, cursor + 42)[0]
        end = (
            cursor
            + _ZIP_CENTRAL_HEADER_BYTES
            + filename_length
            + extra_length
            + member_comment_length
        )
        if end > eocd or filename_length == 0:
            raise ReleaseError("backend-built wheel central member bounds are invalid")
        if disk_start != 0:
            raise ReleaseError("backend-built wheel member starts on another disk")
        if (
            extract_version >= 45
            or compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
        ):
            raise ReleaseError("backend-built wheel member uses unsupported ZIP64 metadata")
        raw_name = original[
            cursor + _ZIP_CENTRAL_HEADER_BYTES :
            cursor + _ZIP_CENTRAL_HEADER_BYTES + filename_length
        ]
        encoding = "utf-8" if flags & 0x800 else "cp437"
        try:
            decoded_name = raw_name.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ReleaseError("backend-built wheel member name encoding is invalid") from exc
        if decoded_name != info.filename:
            raise ReleaseError("backend-built wheel central member ordering or name disagrees")
        _safe_member_name(decoded_name)
        made_by = struct.unpack_from("<H", original, cursor + 4)[0]
        normalized_made_by = (3 << 8) | (made_by & 0xFF)
        struct.pack_into("<H", normalized, cursor + 4, normalized_made_by)
        struct.pack_into("<H", normalized, cursor + 36, 0)
        struct.pack_into(
            "<I",
            normalized,
            cursor + 38,
            _canonical_wheel_external_attr(directory=info.is_dir()),
        )
        changed_ranges.extend(
            ((cursor + 4, cursor + 6), (cursor + 36, cursor + 42))
        )
        cursor = end
    if cursor != eocd or cursor - central_offset != central_size:
        raise ReleaseError("backend-built wheel central directory was not consumed exactly")

    previous = 0
    for start, end in sorted(changed_ranges):
        if original[previous:start] != normalized[previous:start]:
            raise ReleaseError("wheel normalization changed bytes outside central metadata")
        previous = end
    if original[previous:] != normalized[previous:]:
        raise ReleaseError("wheel normalization changed bytes outside central metadata")

    normalized_bytes = bytes(normalized)
    try:
        normalized_archive = zipfile.ZipFile(io.BytesIO(normalized_bytes), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"normalized wheel could not be reopened: {exc}") from exc
    with normalized_archive:
        after_infos = _validate_wheel_members(normalized_archive)
        after_identity = tuple(_wheel_unpatched_identity(info) for info in after_infos)
        if after_identity != before_identity or normalized_archive.comment:
            raise ReleaseError("wheel normalization changed payload or archive identity")
        for info in after_infos:
            expected = _canonical_wheel_external_attr(directory=info.is_dir())
            if (
                info.create_system != 3
                or info.internal_attr != 0
                or info.external_attr != expected
            ):
                raise ReleaseError("wheel normalization did not publish canonical metadata")

    _atomic_write_bytes(wheel, normalized_bytes, mode=0o600)
    persisted = _read_regular_file_bytes(
        wheel,
        label="normalized backend-built wheel",
        maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
    )
    if persisted != normalized_bytes:
        raise ReleaseError("normalized backend-built wheel changed during publication")


def _build_wheel_once(
    snapshot: SourceSnapshot,
    *,
    work_root: Path,
    label: str,
    python: str,
) -> Path:
    stage = work_root / f"source-{label}"
    output = work_root / f"wheel-{label}"
    _copy_snapshot(snapshot, stage)
    output.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(snapshot.source_date_epoch),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    _run(
        [
            python,
            "-I",
            "-B",
            "-c",
            _BUILD_RUNNER_SOURCE,
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(stage),
        ],
        cwd=work_root,
        env=environment,
        timeout=600,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError(f"expected one wheel from build {label}, found {len(wheels)}")
    _canonicalize_built_wheel_metadata(wheels[0])
    return wheels[0]


def build_release(
    source_root: str | Path,
    output_dir: str | Path,
    *,
    allow_dirty: bool = False,
    python: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    """Build twice, compare bytes, verify the wheel, and emit a canonical manifest."""

    snapshot = snapshot_source(source_root, allow_dirty=allow_dirty)
    destination = Path(output_dir).expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=True)
    destination_mode = destination.lstat().st_mode
    if stat.S_ISLNK(destination_mode) or not stat.S_ISDIR(destination_mode):
        raise ReleaseError("release output directory is not a real directory")
    interpreter = str(Path(python).expanduser()) if python is not None else sys.executable
    with tempfile.TemporaryDirectory(prefix="t1000-release-build-") as temporary:
        work_root = Path(temporary)
        first = _build_wheel_once(snapshot, work_root=work_root, label="a", python=interpreter)
        second = _build_wheel_once(snapshot, work_root=work_root, label="b", python=interpreter)
        first_hash = sha256_file(first)
        second_hash = sha256_file(second)
        if first.name != second.name or first_hash != second_hash or first.read_bytes() != second.read_bytes():
            raise ReleaseError(
                "reproducibility gate failed: two controlled builds produced different wheels"
            )
        verified = verify_wheel(first)
        if verified.build_info != snapshot.build_info():
            raise ReleaseError("built wheel does not contain the expected source identity")
        release_id = compute_release_id(str(verified.build_info["build_id"]), first_hash)
        manifest = validate_release_manifest(
            {
                "schema": RELEASE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "build_info": verified.build_info,
                "wheel": first.name,
                "wheel_sha256": first_hash,
                "wheel_size": first.stat().st_size,
            }
        )
        manifest_name = f"{first.stem}.release.json"
        wheel_target = destination / first.name
        manifest_target = destination / manifest_name
        for target in (wheel_target, manifest_target):
            if not _path_lexists(target):
                continue
            mode = target.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ReleaseError(f"release output path is unsafe: {target}")
            if not overwrite:
                raise ReleaseError(f"release output already exists: {target}")
        _atomic_write_bytes(wheel_target, first.read_bytes(), mode=0o444)
        _atomic_write_bytes(manifest_target, canonical_json_bytes(manifest), mode=0o444)
    verify_wheel(wheel_target, manifest_path=manifest_target)
    return {
        "ok": True,
        "action": "release-build",
        "reproducible": True,
        "source": {
            "root": str(snapshot.root),
            "commit": snapshot.source_commit,
            "tree_sha256": snapshot.source_tree_sha256,
            "source_date_epoch": snapshot.source_date_epoch,
            "build_mode": snapshot.build_mode,
            "dirty_entries": list(snapshot.dirty_entries),
        },
        "release_id": release_id,
        "wheel": str(wheel_target),
        "wheel_sha256": first_hash,
        "manifest": str(manifest_target),
        "build_info": snapshot.build_info(),
    }


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name:
        raise ReleaseError(f"unsafe wheel member name: {name!r}")
    pure = PurePosixPath(name)
    if (
        pure.is_absolute()
        or len(pure.parts) > _MAX_WHEEL_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ReleaseError(f"unsafe wheel member path: {name!r}")
    normalized = str(pure)
    if normalized != name.rstrip("/"):
        raise ReleaseError(f"non-canonical wheel member path: {name!r}")
    return normalized


def _wheel_member_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _validate_wheel_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = archive.infolist()
    if not infos or len(infos) > _MAX_WHEEL_ENTRIES:
        raise ReleaseError(f"wheel entry count is unsafe: {len(infos)}")
    names: set[str] = set()
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        if name in names:
            raise ReleaseError(f"wheel contains duplicate member: {name}")
        names.add(name)
        if info.flag_bits & 0x1:
            raise ReleaseError(f"wheel contains encrypted member: {name}")
        if info.file_size < 0 or info.file_size > _MAX_WHEEL_MEMBER_BYTES:
            raise ReleaseError(f"wheel member is too large: {name}")
        total += info.file_size
        if total > _MAX_WHEEL_TOTAL_BYTES:
            raise ReleaseError("wheel uncompressed size exceeds safety limit")
        mode = _wheel_member_mode(info)
        if mode:
            file_type = stat.S_IFMT(mode)
            if info.is_dir():
                if file_type not in {0, stat.S_IFDIR}:
                    raise ReleaseError(f"wheel directory has an unsafe file type: {name}")
            elif file_type not in {0, stat.S_IFREG}:
                raise ReleaseError(f"wheel contains symlink or special file: {name}")
    return tuple(infos)


def _release_requires_canonical_wheel_metadata(version: object) -> bool:
    """Apply strict canonical archive metadata to 0.2.14 and future builds."""

    if not isinstance(version, str):
        return True
    match = _RELEASE_VERSION_PREFIX.fullmatch(version)
    if match is None:
        return True
    try:
        numeric = tuple(int(component) for component in match.groups())
    except ValueError:
        return True
    return numeric >= _CANONICAL_WHEEL_METADATA_MIN_VERSION


def _validate_wheel_permission_metadata(
    infos: Sequence[zipfile.ZipInfo],
    *,
    version: object,
    dist_info: str,
) -> None:
    strict = _release_requires_canonical_wheel_metadata(version)
    legacy_record = f"{dist_info}/RECORD"
    for info in infos:
        name = _safe_member_name(info.filename)
        if strict:
            expected_attrs = {_canonical_wheel_external_attr(directory=info.is_dir())}
            policy = "canonical"
        elif info.is_dir():
            expected_attrs = {_canonical_wheel_external_attr(directory=True)}
            policy = "legacy"
        elif name == legacy_record:
            expected_attrs = {
                (stat.S_IFREG | 0o644) << 16,
                (stat.S_IFREG | 0o664) << 16,
            }
            policy = "legacy"
        else:
            expected_attrs = {(stat.S_IFREG | 0o644) << 16}
            policy = "legacy"
        if (
            info.create_system != 3
            or info.internal_attr != 0
            or info.external_attr not in expected_attrs
        ):
            expected_text = ",".join(f"{value:#x}" for value in sorted(expected_attrs))
            raise ReleaseError(
                f"{policy} wheel metadata mismatch for {name}: "
                f"creator={info.create_system}, internal={info.internal_attr}, "
                f"external={info.external_attr:#x}, expected={expected_text}"
            )


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _wheel_component(value: str) -> str:
    """Return the normalized version component used by wheel/dist-info names."""

    return re.sub(r"-+", "_", value)


def _validate_entry_points(content: str) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise ReleaseError(f"wheel entry_points.txt is invalid: {exc}") from exc
    if parser.sections() != ["console_scripts"]:
        raise ReleaseError("wheel entry_points.txt must contain only [console_scripts]")
    entries = {name.strip(): value.strip() for name, value in parser.items("console_scripts")}
    if entries != {"t1000": "termdeck.cli:main"}:
        raise ReleaseError(
            "wheel entry_points.txt must expose exactly t1000 = termdeck.cli:main"
        )


def _validate_wheel_metadata(
    *,
    wheel: Path,
    dist_info: str,
    version: str,
    content: str,
    files: Mapping[str, zipfile.ZipInfo],
) -> None:
    normalized_version = _wheel_component(version)
    expected_dist_info = f"t1000-{normalized_version}.dist-info"
    expected_wheel = f"t1000-{normalized_version}-py3-none-any.whl"
    if dist_info != expected_dist_info:
        raise ReleaseError(
            f"wheel dist-info directory does not match distribution/version: {dist_info}"
        )
    if wheel.name != expected_wheel:
        raise ReleaseError(
            f"wheel filename does not match the supported pure-Python tag: {wheel.name}"
        )
    metadata = Parser().parsestr(content)
    wheel_version = str(metadata.get("Wheel-Version", "")).strip()
    if not re.fullmatch(r"1(?:\.\d+)?", wheel_version):
        raise ReleaseError(f"unsupported Wheel-Version: {wheel_version!r}")
    if str(metadata.get("Root-Is-Purelib", "")).strip().casefold() != "true":
        raise ReleaseError("t1000 release wheel must be Root-Is-Purelib: true")
    tags = [str(value).strip() for value in metadata.get_all("Tag", [])]
    if tags != ["py3-none-any"]:
        raise ReleaseError(f"t1000 release wheel must have exactly Tag: py3-none-any; got {tags}")
    allowed_prefixes = ("termdeck/", f"{dist_info}/")
    unexpected = sorted(name for name in files if not name.startswith(allowed_prefixes))
    if unexpected:
        raise ReleaseError(f"wheel contains unexpected top-level members: {unexpected[:10]}")


def _read_regular_file_bytes(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    """Read one final-path regular file through a no-follow descriptor."""

    source = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ReleaseError(f"could not open {label} safely: {source}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseError(f"{label} is not a regular file: {source}")
        if before.st_size < 0 or before.st_size > maximum_bytes:
            raise ReleaseError(f"{label} exceeds the {maximum_bytes}-byte safety limit")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ReleaseError(f"{label} changed or truncated while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseError(f"{label} grew while being read")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_after != identity_before:
            raise ReleaseError(f"{label} changed while being verified")
        content = b"".join(chunks)
        if len(content) != before.st_size:
            raise ReleaseError(f"{label} size changed while being verified")
        return content
    finally:
        os.close(descriptor)


def _open_real_directory_at(
    path: str | Path,
    *,
    label: str,
    dir_fd: int | None = None,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ReleaseError(f"could not open {label} as a real directory: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ReleaseError(f"{label} is not a real directory")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_immutable_file_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name in {".", ".."}
    ):
        raise ReleaseError(f"{label} has an unsafe direct-child name")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ReleaseError(f"could not open {label} safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseError(f"{label} is not a regular file")
        if stat.S_IMODE(before.st_mode) & 0o222 or before.st_nlink != 1:
            raise ReleaseError(f"{label} is writable or hard-linked")
        if before.st_size < 0 or before.st_size > maximum_bytes:
            raise ReleaseError(f"{label} exceeds the {maximum_bytes}-byte safety limit")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ReleaseError(f"{label} changed or truncated while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseError(f"{label} grew while being read")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
            stat.S_IMODE(before.st_mode),
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
            stat.S_IMODE(after.st_mode),
        )
        if identity_after != identity_before:
            raise ReleaseError(f"{label} changed while being read")
        content = b"".join(chunks)
        if len(content) != before.st_size:
            raise ReleaseError(f"{label} size changed while being read")
        return content
    finally:
        os.close(descriptor)


def _read_activation_artifact_snapshot(
    layout: ReleaseLayout,
    release_id: str,
) -> _ActivationArtifactSnapshot:
    """Read manifest and wheel through a no-follow descriptor chain.

    Only the activation artifacts are required. The extracted site may be
    contaminated and remain repairable, but neither the managed root, release,
    artifacts directory, manifest, nor wheel can be redirected through symlinks.
    """

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("activation target release id is unsafe")
    root_fd = _open_real_directory_at(layout.root, label="managed release root")
    releases_fd: int | None = None
    release_fd: int | None = None
    artifacts_fd: int | None = None
    try:
        releases_fd = _open_real_directory_at(
            "releases",
            label="managed releases directory",
            dir_fd=root_fd,
        )
        release_fd = _open_real_directory_at(
            release_id,
            label="activation target release",
            dir_fd=releases_fd,
        )
        manifest_bytes = _read_immutable_file_at(
            release_fd,
            "manifest.json",
            label="activation target manifest",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        try:
            manifest = validate_release_manifest(json.loads(manifest_bytes.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, BuildIdentityError) as exc:
            raise ReleaseError(f"activation target manifest is invalid: {exc}") from exc
        _require_production_build(
            manifest["build_info"],
            label="activation target",
        )
        if manifest_bytes != canonical_json_bytes(manifest):
            raise ReleaseError("activation target manifest is not canonical")
        if manifest.get("release_id") != release_id:
            raise ReleaseError("activation target manifest and release id disagree")
        wheel_name = manifest.get("wheel")
        if not isinstance(wheel_name, str):
            raise ReleaseError("activation target manifest has no wheel name")
        artifacts_fd = _open_real_directory_at(
            "artifacts",
            label="activation target artifacts directory",
            dir_fd=release_fd,
        )
        wheel_bytes = _read_immutable_file_at(
            artifacts_fd,
            wheel_name,
            label="activation target wheel",
            maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
        )
        if (
            len(wheel_bytes) != manifest.get("wheel_size")
            or sha256_bytes(wheel_bytes) != manifest.get("wheel_sha256")
        ):
            raise ReleaseError("activation target wheel hash or size does not match manifest")
        return _ActivationArtifactSnapshot(
            manifest=dict(manifest),
            manifest_bytes=manifest_bytes,
            wheel_name=wheel_name,
            wheel_bytes=wheel_bytes,
        )
    finally:
        for descriptor in (artifacts_fd, release_fd, releases_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _verified_wheel_bytes(verified: VerifiedWheel) -> bytes:
    content = _read_regular_file_bytes(
        verified.path,
        label="verified wheel",
        maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
    )
    if len(content) != verified.size or sha256_bytes(content) != verified.sha256:
        raise ReleaseError("verified wheel changed after its evidence receipt was created")
    return content


def verify_wheel(
    wheel_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
) -> VerifiedWheel:
    wheel = Path(wheel_path).expanduser().absolute()
    if wheel.suffix != ".whl":
        raise ReleaseError(f"not a wheel file: {wheel}")
    wheel_content = _read_regular_file_bytes(
        wheel,
        label="wheel artifact",
        maximum_bytes=_MAX_WHEEL_ARCHIVE_BYTES,
    )
    wheel_hash = sha256_bytes(wheel_content)
    wheel_size = len(wheel_content)
    try:
        archive = zipfile.ZipFile(io.BytesIO(wheel_content), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"could not open wheel: {exc}") from exc
    with archive:
        infos = _validate_wheel_members(archive)
        files = {info.filename.rstrip("/"): info for info in infos if not info.is_dir()}
        dist_infos = sorted(
            {
                name.split("/", 1)[0]
                for name in files
                if ".dist-info/" in name and name.split("/", 1)[0].endswith(".dist-info")
            }
        )
        if len(dist_infos) != 1:
            raise ReleaseError(f"wheel must contain one dist-info directory, found {dist_infos}")
        dist_info = dist_infos[0]
        metadata_name = f"{dist_info}/METADATA"
        wheel_metadata_name = f"{dist_info}/WHEEL"
        entry_points_name = f"{dist_info}/entry_points.txt"
        record_name = f"{dist_info}/RECORD"
        build_info_name = "termdeck/_build_info.json"
        required = {
            "termdeck/__init__.py",
            "termdeck/cli.py",
            metadata_name,
            wheel_metadata_name,
            entry_points_name,
            record_name,
            build_info_name,
        }
        missing = sorted(required - set(files))
        if missing:
            raise ReleaseError(f"wheel is missing required members: {missing}")

        try:
            metadata_text = archive.read(metadata_name).decode("utf-8")
            wheel_metadata_text = archive.read(wheel_metadata_name).decode("utf-8")
            entry_points = archive.read(entry_points_name).decode("utf-8")
            build_info_text = archive.read(build_info_name).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError("wheel metadata must be valid UTF-8") from exc
        metadata = Parser().parsestr(metadata_text)
        distribution = str(metadata.get("Name", "")).strip()
        version = str(metadata.get("Version", "")).strip()
        if distribution.casefold().replace("_", "-") != "t1000" or not version:
            raise ReleaseError("wheel METADATA does not identify a versioned t1000 distribution")
        _validate_wheel_permission_metadata(
            infos,
            version=version,
            dist_info=dist_info,
        )
        _validate_wheel_metadata(
            wheel=wheel,
            dist_info=dist_info,
            version=version,
            content=wheel_metadata_text,
            files=files,
        )
        _validate_entry_points(entry_points)
        try:
            build_info = validate_build_info(json.loads(build_info_text))
        except (json.JSONDecodeError, BuildIdentityError) as exc:
            raise ReleaseError(f"wheel build info is invalid: {exc}") from exc
        if build_info["version"] != version:
            raise ReleaseError("wheel build info and METADATA versions disagree")
        try:
            package_init = archive.read("termdeck/__init__.py").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError("termdeck/__init__.py must be valid UTF-8") from exc
        version_match = re.search(
            r"^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$",
            package_init,
            re.MULTILINE,
        )
        if version_match is None or version_match.group(1) != version:
            raise ReleaseError("wheel package __version__ and METADATA versions disagree")

        try:
            record_text = archive.read(record_name).decode("utf-8")
            record_rows = tuple(tuple(row) for row in csv.reader(io.StringIO(record_text)))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ReleaseError(f"wheel RECORD is invalid: {exc}") from exc
        if any(len(row) != 3 for row in record_rows):
            raise ReleaseError("wheel RECORD rows must have exactly three fields")
        record_map: dict[str, tuple[str, str]] = {}
        for path, digest, size in record_rows:
            normalized = _safe_member_name(path)
            if normalized in record_map:
                raise ReleaseError(f"wheel RECORD contains duplicate path: {normalized}")
            record_map[normalized] = (digest, size)
        if set(record_map) != set(files):
            missing_record = sorted(set(files) - set(record_map))
            extra_record = sorted(set(record_map) - set(files))
            raise ReleaseError(
                f"wheel RECORD coverage mismatch; missing={missing_record}, extra={extra_record}"
            )
        for name, info in files.items():
            digest, size_text = record_map[name]
            content = archive.read(info)
            if name == record_name:
                if digest or size_text:
                    raise ReleaseError("wheel RECORD must leave its own hash and size empty")
                continue
            if digest != _record_hash(content):
                raise ReleaseError(f"wheel RECORD hash mismatch: {name}")
            if size_text != str(len(content)):
                raise ReleaseError(f"wheel RECORD size mismatch: {name}")

    manifest: dict[str, object] | None = None
    if manifest_path is not None:
        source = Path(manifest_path).expanduser().absolute()
        raw = _read_regular_file_bytes(
            source,
            label="release manifest",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        try:
            manifest = validate_release_manifest(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, BuildIdentityError) as exc:
            raise ReleaseError(f"release manifest is invalid: {exc}") from exc
        if raw != canonical_json_bytes(manifest):
            raise ReleaseError("release manifest is not canonically serialized")
        if manifest["wheel"] != wheel.name:
            raise ReleaseError("manifest wheel name does not match artifact")
        if manifest["wheel_sha256"] != wheel_hash or manifest["wheel_size"] != wheel_size:
            raise ReleaseError("manifest wheel hash or size does not match artifact")
        if manifest["build_info"] != build_info:
            raise ReleaseError("manifest build info does not match bundled build info")

    return VerifiedWheel(
        path=wheel,
        sha256=wheel_hash,
        size=wheel_size,
        distribution=distribution,
        version=version,
        dist_info=dist_info,
        build_info=build_info,
        record_rows=record_rows,
        members=tuple(sorted(files)),
        manifest=manifest,
    )


@dataclass(frozen=True)
class _DirectoryIdentity:
    """The stable identity of one directory in a publication path."""

    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _FileIdentity:
    """A file identity that also detects a hard-link or in-place replacement."""

    device: int
    inode: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _DirectoryGuard:
    """A path's complete, no-follow directory ancestry at preflight time."""

    path: Path
    chain: tuple[_DirectoryIdentity, ...]


@dataclass(frozen=True)
class _PublicationParentGuards:
    """Reusable parent identities shared across wrapper preflight and rollback."""

    guards: tuple[_DirectoryGuard, ...]

    def for_path(self, path: Path) -> _DirectoryGuard | None:
        parent = path.parent
        for guard in self.guards:
            if guard.path == parent:
                return guard
        return None


@dataclass
class _DirectoryAnchor:
    """An opened parent directory plus the path identity it was opened from."""

    path: Path
    descriptor: int
    chain: tuple[_DirectoryIdentity, ...]
    label: str

    @property
    def guard(self) -> _DirectoryGuard:
        return _DirectoryGuard(self.path, self.chain)

    def assert_current(self, *, phase: str) -> None:
        """Reject a renamed, replaced, or symlinked ancestor before mutation."""

        try:
            current = _directory_identity(os.fstat(self.descriptor), label=self.label)
        except OSError as exc:
            raise ReleaseError(
                f"publication parent descriptor is unavailable {phase}: {self.path}: {exc}"
            ) from exc
        if current != self.chain[-1]:
            raise ReleaseError(f"publication parent identity changed {phase}: {self.path}")
        fresh = _open_directory_anchor(self.path, label=self.label, create_parents=False)
        try:
            if fresh.chain != self.chain:
                raise ReleaseError(f"publication parent identity changed {phase}: {self.path}")
        finally:
            fresh.close()

    def fsync(self, *, label: str) -> None:
        _fsync_descriptor(self.descriptor, label=f"{label}: {self.path}")

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            os.close(descriptor)
        except OSError:
            # Closing an owned descriptor is best effort only.  Publication
            # decisions have already been made from its identity.
            pass


class _PublicationParents:
    """Own descriptor-anchored parents for every file in one transaction."""

    def __init__(self, anchors: Mapping[Path, _DirectoryAnchor]) -> None:
        self._anchors = dict(anchors)

    def for_path(self, path: Path) -> _DirectoryAnchor:
        try:
            return self._anchors[path.parent]
        except KeyError as exc:  # pragma: no cover - built from the write set
            raise ReleaseError(f"publication parent is unavailable: {path.parent}") from exc

    def assert_current(self, *, phase: str) -> None:
        for path in sorted(self._anchors, key=str):
            self._anchors[path].assert_current(phase=phase)

    def anchors(self) -> Mapping[Path, _DirectoryAnchor]:
        return dict(self._anchors)

    def close(self) -> None:
        for anchor in self._anchors.values():
            anchor.close()


@dataclass(frozen=True)
class _AnchoredFileSnapshot:
    snapshot: _FileSnapshot
    identity: _FileIdentity | None
    sha256: str | None = None


@dataclass(frozen=True)
class PublicationTransactionReceipt:
    """A durable hand-off point for a descriptor publication transaction.

    ``before_publish`` callbacks receive an ``intent`` receipt after the
    create-exclusive journal and all authenticated preimages have reached
    storage, but before the first public rename.  ``after_publish`` callbacks
    receive a ``published`` receipt after exact postimages have been captured
    and verified, while that same journal is still live.  Consumers can
    persist either receipt and call :func:`recover_publication_transaction`
    after an abrupt exit.  ``after_recovery`` receives ``recovered`` only
    after a journaled inverse has restored exact current leaves, and before
    that journal is terminalized.  The mappings are immutable so a callback
    cannot silently alter the transaction's ownership proof.
    """

    journal_path: Path
    journal_identity: _FileIdentity
    state: str
    parent_guards: _PublicationParentGuards
    preimages: Mapping[Path, _AnchoredFileSnapshot]
    postimages: Mapping[Path, _AnchoredFileSnapshot]


@dataclass
class _StagedPublicationFile:
    parent: _DirectoryAnchor
    temporary_name: str
    descriptor: int
    identity: _FileIdentity
    label: str

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            os.close(descriptor)
        except OSError:
            pass


@dataclass
class _PublicationRecord:
    path: Path
    name: str
    content: bytes | None
    mode: int
    label: str
    snapshot: _AnchoredFileSnapshot
    staged: _StagedPublicationFile | None = None
    # For a replacement or removal, the former public leaf remains under a
    # fresh private name until verification completes.  This makes rollback a
    # descriptor-relative rename instead of a stale-name overwrite.
    quarantine: _StagedPublicationFile | None = None
    committed_identity: _FileIdentity | None = None
    deleted: bool = False
    # These names and identities deliberately outlive their open descriptors.
    # They are the durable recovery vocabulary written to the transaction
    # journal before each rename.
    staged_name: str | None = None
    staged_identity: _FileIdentity | None = None
    quarantine_name: str | None = None
    quarantine_identity: _FileIdentity | None = None
    rollback_name: str | None = None
    rollback_identity: _FileIdentity | None = None
    rollback: _StagedPublicationFile | None = None
    # A private leaf is never unlinked at its checked name.  It first moves to
    # this registered disposal name, so restart recovery can finish or retain
    # the obligation if a signal lands between the move and unlink/fsync.
    disposal_name: str | None = None
    disposal_identity: _FileIdentity | None = None
    disposal_source: str | None = None
    disposal: _StagedPublicationFile | None = None
    phase: str = "prepared"


def _fsync_descriptor(descriptor: int, *, label: str) -> None:
    """Fsync one owned descriptor, retrying only bounded EINTR interruptions."""

    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise ReleaseError(f"could not fsync {label}: {exc}") from exc
        if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
            break
    raise ReleaseError(
        f"could not fsync {label}: interrupted {_PUBLICATION_EINTR_RETRIES} times"
    )


_PUBLICATION_JOURNAL_FRAME = b"TDJ1:"


def _journal_file_identity_payload(identity: _FileIdentity | None) -> dict[str, int] | None:
    if identity is None:
        return None
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "nlink": identity.nlink,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
    }


def _journal_file_identity_from_payload(value: object, *, label: str) -> _FileIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "device", "inode", "mode", "nlink", "size", "mtime_ns", "ctime_ns"
    }:
        raise ReleaseError(f"{label} identity is malformed")
    values: dict[str, int] = {}
    for name in ("device", "inode", "mode", "nlink", "size", "mtime_ns", "ctime_ns"):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ReleaseError(f"{label} identity is malformed")
        values[name] = item
    identity = _FileIdentity(
        values["device"],
        values["inode"],
        values["mode"],
        values["nlink"],
        values["size"],
        values["mtime_ns"],
        values["ctime_ns"],
    )
    if (
        identity.device < 0
        or identity.inode <= 0
        or identity.nlink != 1
        or identity.size < 0
        or identity.mode < 0
        or identity.mode > 0o7777
    ):
        raise ReleaseError(f"{label} identity is malformed")
    return identity


def _journal_snapshot_payload(snapshot: _AnchoredFileSnapshot) -> dict[str, object]:
    value: dict[str, object] = {
        "exists": snapshot.snapshot.exists,
        "mode": snapshot.snapshot.mode,
        "identity": _journal_file_identity_payload(snapshot.identity),
        "sha256": snapshot.sha256,
    }
    if snapshot.snapshot.exists:
        value["content_b64"] = base64.b64encode(snapshot.snapshot.content).decode("ascii")
    else:
        value["content_b64"] = None
    return value


def _journal_snapshot_from_payload(value: object, *, label: str) -> _AnchoredFileSnapshot:
    expected = {"exists", "mode", "identity", "sha256", "content_b64"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ReleaseError(f"{label} snapshot is malformed")
    exists = value["exists"]
    mode = value["mode"]
    encoded = value["content_b64"]
    digest = value["sha256"]
    if not isinstance(exists, bool) or isinstance(mode, bool) or not isinstance(mode, int):
        raise ReleaseError(f"{label} snapshot is malformed")
    if mode < 0 or mode > 0o7777:
        raise ReleaseError(f"{label} snapshot is malformed")
    identity = _journal_file_identity_from_payload(value["identity"], label=label)
    if not exists:
        if encoded is not None or digest is not None or identity is not None:
            raise ReleaseError(f"{label} absent snapshot is malformed")
        return _AnchoredFileSnapshot(_FileSnapshot(False), None, None)
    if not isinstance(encoded, str) or not isinstance(digest, str) or _RELEASE_ID.fullmatch(digest) is None:
        raise ReleaseError(f"{label} snapshot is malformed")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ReleaseError(f"{label} snapshot is malformed") from exc
    if identity is None or identity.size != len(content) or identity.mode != mode:
        raise ReleaseError(f"{label} snapshot is malformed")
    if sha256_bytes(content) != digest:
        raise ReleaseError(f"{label} snapshot is malformed")
    return _AnchoredFileSnapshot(_FileSnapshot(True, content, mode), identity, digest)


def _journal_directory_guard_payload(anchor: _DirectoryAnchor) -> dict[str, object]:
    return {
        "path": str(anchor.path),
        "chain": [
            {"device": identity.device, "inode": identity.inode, "mode": identity.mode}
            for identity in anchor.chain
        ],
    }


def _journal_directory_guards_from_payload(value: object) -> _PublicationParentGuards:
    if not isinstance(value, list) or not value:
        raise ReleaseError("publication journal parent guards are malformed")
    guards: list[_DirectoryGuard] = []
    seen: set[Path] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "chain"}:
            raise ReleaseError("publication journal parent guards are malformed")
        raw_path = item["path"]
        if not isinstance(raw_path, str):
            raise ReleaseError("publication journal parent guard path is malformed")
        path = _canonical_publication_path(raw_path, label="publication journal parent")
        chain_value = item["chain"]
        if not isinstance(chain_value, list) or not chain_value:
            raise ReleaseError("publication journal parent guard is malformed")
        chain: list[_DirectoryIdentity] = []
        for identity_value in chain_value:
            if not isinstance(identity_value, Mapping) or set(identity_value) != {
                "device", "inode", "mode"
            }:
                raise ReleaseError("publication journal parent guard is malformed")
            numbers: list[int] = []
            for name in ("device", "inode", "mode"):
                number = identity_value[name]
                if isinstance(number, bool) or not isinstance(number, int):
                    raise ReleaseError("publication journal parent guard is malformed")
                numbers.append(number)
            directory = _DirectoryIdentity(numbers[0], numbers[1], numbers[2])
            if directory.device < 0 or directory.inode <= 0 or directory.mode < 0 or directory.mode > 0o7777:
                raise ReleaseError("publication journal parent guard is malformed")
            chain.append(directory)
        if path in seen:
            raise ReleaseError("publication journal repeats a parent guard")
        seen.add(path)
        guards.append(_DirectoryGuard(path, tuple(chain)))
    return _PublicationParentGuards(tuple(guards))


def _journal_record_payload(record: _PublicationRecord) -> dict[str, object]:
    return {
        "path": str(record.path),
        "name": record.name,
        "content_b64": (
            base64.b64encode(record.content).decode("ascii")
            if record.content is not None
            else None
        ),
        "mode": record.mode,
        "label": record.label,
        "snapshot": _journal_snapshot_payload(record.snapshot),
        "staged_name": record.staged_name,
        "staged_identity": _journal_file_identity_payload(record.staged_identity),
        "quarantine_name": record.quarantine_name,
        "quarantine_identity": _journal_file_identity_payload(record.quarantine_identity),
        "rollback_name": record.rollback_name,
        "rollback_identity": _journal_file_identity_payload(record.rollback_identity),
        "disposal_name": record.disposal_name,
        "disposal_identity": _journal_file_identity_payload(record.disposal_identity),
        "disposal_source": record.disposal_source,
        "committed_identity": _journal_file_identity_payload(record.committed_identity),
        "deleted": record.deleted,
        "phase": record.phase,
    }


def _publication_journal_name(name: str) -> str:
    _safe_component(name, label="publication journal")
    if (
        not name.startswith(_PUBLICATION_JOURNAL_PREFIX)
        or not name.endswith(_PUBLICATION_JOURNAL_SUFFIX)
        or len(name) <= len(_PUBLICATION_JOURNAL_PREFIX) + len(_PUBLICATION_JOURNAL_SUFFIX)
    ):
        raise ReleaseError("publication journal name is invalid")
    return name


def _publication_journal_frame(payload: Mapping[str, object]) -> bytes:
    encoded = canonical_json_bytes(payload)
    if len(encoded) > _MAX_PUBLICATION_JOURNAL_FRAME_BYTES:
        raise ReleaseError("publication journal is too large")
    digest = sha256_bytes(encoded).encode("ascii")
    return (
        _PUBLICATION_JOURNAL_FRAME
        + f"{len(encoded):08x}".encode("ascii")
        + b":"
        + digest
        + b"\n"
        + encoded
    )


def _publication_journal_latest_payload(content: bytes) -> dict[str, object]:
    """Read the newest complete append-only journal frame after an interruption."""

    latest: dict[str, object] | None = None
    latest_generation = -1
    seen_generations: set[int] = set()
    offset = 0
    header_size = len(_PUBLICATION_JOURNAL_FRAME) + 8 + 1 + 64 + 1
    while offset < len(content):
        if len(content) - offset < header_size:
            # A torn append may stop in the header; its preceding frame is
            # still authoritative.  Do not silently tolerate arbitrary tail
            # bytes, however: a torn writer can only have begun the fixed
            # frame marker at this offset.
            if content[offset:].startswith(_PUBLICATION_JOURNAL_FRAME):
                break
            raise ReleaseError("publication journal frame order is malformed")
        if content[offset:offset + len(_PUBLICATION_JOURNAL_FRAME)] != _PUBLICATION_JOURNAL_FRAME:
            raise ReleaseError("publication journal frame order is malformed")
        size_raw = content[offset + len(_PUBLICATION_JOURNAL_FRAME): offset + len(_PUBLICATION_JOURNAL_FRAME) + 8]
        separator = content[offset + len(_PUBLICATION_JOURNAL_FRAME) + 8]
        digest_start = offset + len(_PUBLICATION_JOURNAL_FRAME) + 9
        digest = content[digest_start:digest_start + 64]
        payload_start = digest_start + 65
        if separator != ord(":") or content[payload_start - 1:payload_start] != b"\n":
            raise ReleaseError("publication journal frame is malformed")
        try:
            size = int(size_raw.decode("ascii"), 16)
        except (UnicodeDecodeError, ValueError):
            raise ReleaseError("publication journal frame is malformed") from None
        if size <= 0 or size > _MAX_PUBLICATION_JOURNAL_FRAME_BYTES:
            raise ReleaseError("publication journal frame is malformed")
        payload_end = payload_start + size
        if payload_end > len(content):
            # The final payload may be only partly appended.  Do not parse a
            # later marker embedded in it as a second independent frame.
            break
        payload_raw = content[payload_start:payload_end]
        if sha256_bytes(payload_raw).encode("ascii") != digest:
            raise ReleaseError("publication journal frame digest is malformed")
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReleaseError("publication journal frame payload is malformed") from None
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != payload_raw:
            raise ReleaseError("publication journal frame payload is malformed")
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ReleaseError("publication journal frame generation is malformed")
        # A failed fsync can leave the writer unsure whether a complete frame
        # reached stable storage.  Generation reuse would make the reader
        # choose between two equally authoritative frames.  Treat duplicate
        # or out-of-order complete frames as tampering/ambiguous recovery,
        # rather than guessing which operation owned a leaf.
        if generation in seen_generations or generation <= latest_generation:
            raise ReleaseError("publication journal has ambiguous frame generation")
        seen_generations.add(generation)
        latest = payload
        latest_generation = generation
        offset = payload_end
    if latest is None:
        raise ReleaseError("publication journal has no complete durable record")
    return latest


@dataclass
class _PublicationJournal:
    """Append-only write-ahead record for one descriptor-rooted transaction.

    A mutation is preceded by a fully fsynced intent frame.  An interrupted
    append leaves a partial frame, while the preceding complete frame remains
    parseable.  This avoids a truncate/rewrite window in the recovery record.
    """

    parent: _DirectoryAnchor
    name: str
    descriptor: int
    records: list[_PublicationRecord]
    anchors: Mapping[Path, _DirectoryAnchor]
    generation: int = 0
    # ``state`` is the latest state whose frame fsync completed.  An append
    # can have reached storage before fsync reports an error, so the attempted
    # state is deliberately separate and blocks all in-process conclusions
    # until a fresh reader selects the ordered durable frame on restart.
    state: str = "active"
    attempted_state: str | None = None
    durability_uncertain: bool = False

    @property
    def path(self) -> Path:
        return self.parent.path / self.name

    @classmethod
    def create(
        cls,
        parents: _PublicationParents,
        records: list[_PublicationRecord],
        *,
        journal_path: Path | str | os.PathLike[str] | None = None,
    ) -> "_PublicationJournal":
        if not records:
            raise ReleaseError("publication journal has no records")
        if journal_path is None:
            parent = parents.for_path(records[0].path)
            name = (
                f"{_PUBLICATION_JOURNAL_PREFIX}{os.getpid()}-"
                f"{uuid.uuid4().hex}{_PUBLICATION_JOURNAL_SUFFIX}"
            )
        else:
            path = _canonical_publication_path(journal_path, label="publication journal")
            parent = parents.for_path(path)
            name = _publication_journal_name(path.name)
        _publication_journal_name(name)
        parent.assert_current(phase="before journal creation")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | _nofollow_open_flag()
        )
        descriptor = _openat(
            parent.descriptor,
            name,
            flags,
            mode=0o600,
            label="publication journal",
        )
        result: _PublicationJournal | None = None
        created_identity: _FileIdentity | None = None

        def authenticated_created_leaf() -> _FileIdentity:
            """Return the exact O_EXCL leaf, never a later name occupant."""

            nonlocal created_identity
            parent.assert_current(phase="during journal creation failure handling")
            descriptor_identity = _file_identity(
                os.fstat(descriptor),
                label="publication journal",
            )
            if descriptor_identity.nlink != 1:
                raise ReleaseError("publication journal created leaf has unexpected hard links")
            if created_identity is not None and not _same_created_journal_leaf(
                descriptor_identity,
                created_identity,
            ):
                raise ReleaseError("publication journal descriptor changed during creation")
            named = _validate_regular_at(parent, name, label="publication journal")
            if (
                named is None
                or not _same_created_journal_leaf(named, descriptor_identity)
            ):
                raise ReleaseError("publication journal changed during creation")
            return descriptor_identity

        def abort_pre_result_failure(cause: BaseException) -> NoReturn:
            """Resolve a failed O_EXCL setup without leaving a blank journal.

            ``O_CREAT|O_EXCL`` has already made a directory entry when any of
            fchmod/fstat/name validation fails.  Do not close the FD and leave
            a zero-byte journal behind.  We first authenticate the original
            O_EXCL inode through the same anchored directory FD, then write a
            valid active failure frame.  Once that frame and its parent have
            been durably synchronized, cleanup is deliberately deferred to
            restart recovery: moving the only named receipt to a disposal name
            and then losing its directory fsync would otherwise leave no
            stable path by which a caller could discover it.  A replacement
            name is never adopted or unlinked.
            """

            failure_journal: _PublicationJournal | None = None
            created_for_cleanup: _FileIdentity | None = None
            try:
                identity = authenticated_created_leaf()
                created_for_cleanup = identity
                # A failed fchmod must not leave a retained journal whose
                # mode the restart reader cannot consume.  The exact
                # O_EXCL descriptor remains available for descriptor-bound
                # cleanup, so this pre-mutation path can fail closed.
                if identity.mode & ~0o600 == 0:
                    if identity.mode != 0o600:
                        os.fchmod(descriptor, 0o600)
                        identity = authenticated_created_leaf()
                    if identity.mode != 0o600:
                        raise ReleaseError("publication journal mode is unsafe during recovery setup")
                    failure_journal = cls(
                        parent,
                        name,
                        descriptor,
                        records,
                        parents.anchors(),
                    )
                    try:
                        failure_journal.persist()
                        parent.fsync(label="publication journal failure receipt parent")
                    except BaseException as receipt_exc:
                        # A complete frame may have reached storage despite a
                        # failing fsync.  Retain the exact named leaf whenever
                        # it still authenticates; restart parsing, rather than
                        # this process, decides whether it is usable.
                        try:
                            authenticated_created_leaf()
                        except BaseException:
                            failure_journal.close()
                            raise _PublicationJournalPreflightError(
                                "publication journal setup failed before mutation; "
                                "its failure receipt could not be authenticated",
                                cause=cause,
                            ) from receipt_exc
                        else:
                            raise _PublicationJournalCreateError(
                                failure_journal,
                                receipt_exc,
                                original_cause=(
                                    receipt_exc
                                    if not isinstance(receipt_exc, Exception)
                                    else cause
                                ),
                            ) from cause

                    # The receipt itself is now the exact, durable recovery
                    # handle.  Do not attempt a local rename/unlink of the
                    # only known name: a post-unlink parent-fsync failure has
                    # no reliable answer about whether that name will survive
                    # a crash.  Recovery sees an active journal with no public
                    # mutation, terminalizes it, and performs the same
                    # descriptor-bound private cleanup after that decision.
                    raise _PublicationJournalCreateError(failure_journal, cause)

                staged = _StagedPublicationFile(
                    parent,
                    name,
                    descriptor,
                    identity,
                    "publication journal",
                )
                try:
                    _cleanup_staged_file(staged)
                except BaseException as cleanup_exc:
                    # An unsafe-mode O_EXCL leaf cannot hold a full receipt.
                    # The exact cleanup helper nevertheless uses a parent FD,
                    # compares the original inode, and fsyncs that parent;
                    # a failure here is never described as a rollback.
                    raise _PublicationJournalPreflightError(
                        "publication journal setup failed before mutation and "
                        "its cleanup durability is uncertain",
                        cause=cause,
                    ) from cleanup_exc
                staged.close()
            except _PublicationJournalCreateError:
                raise
            except _PublicationJournalPreflightError:
                raise
            except BaseException as cleanup_auth_exc:
                # Without an exact FD/name identity, unlinking would be a
                # stale-name delete.  Preserve any foreign replacement and
                # report a pre-mutation failure instead of claiming rollback.
                cleanup_error: BaseException | None = None
                if created_for_cleanup is not None:
                    try:
                        _cleanup_staged_file(
                            _StagedPublicationFile(
                                parent,
                                name,
                                descriptor,
                                created_for_cleanup,
                                "publication journal",
                            )
                        )
                    except BaseException as exc:
                        cleanup_error = exc
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                if not isinstance(cause, Exception):
                    raise cause
                if cleanup_error is not None:
                    raise _PublicationJournalPreflightError(
                        "publication journal setup failed before mutation and "
                        "its cleanup durability is uncertain",
                        cause=cause,
                    ) from cleanup_error
                raise _PublicationJournalPreflightError(
                    "publication journal setup failed before mutation; its "
                    f"created leaf could not be authenticated for cleanup: {cleanup_auth_exc}",
                    cause=cause,
                ) from cause
            if not isinstance(cause, Exception):
                raise cause
            raise _PublicationJournalPreflightError(
                "publication journal setup failed before mutation; incomplete "
                "journal cleanup completed",
                cause=cause,
            ) from cause
        try:
            created_identity = _file_identity(
                os.fstat(descriptor),
                label="publication journal",
            )
            if created_identity.nlink != 1:
                raise ReleaseError("publication journal created leaf has unexpected hard links")
            os.fchmod(descriptor, 0o600)
            identity = authenticated_created_leaf()
            if identity.mode != 0o600:
                raise ReleaseError("publication journal mode is unsafe during creation")
            try:
                result = cls(parent, name, descriptor, records, parents.anchors())
                result.persist()
                parent.fsync(label="publication journal parent")
            except BaseException as exc:
                # The file, initial frame, or directory entry may be durable
                # despite this error.  Keep the exact handle and never let a
                # caller classify this as a normal pre-mutation rollback.
                if result is None:  # pragma: no cover - construction above
                    raise
                raise _PublicationJournalCreateError(result, exc) from exc
            return result
        except _PublicationJournalCreateError:
            raise
        except BaseException as exc:
            if result is not None:
                result.close()
                raise
            abort_pre_result_failure(exc)

    def _payload(self, *, state: str | None = None) -> dict[str, object]:
        return {
            "schema": _PUBLICATION_JOURNAL_SCHEMA,
            "generation": self.generation,
            "state": self.state if state is None else state,
            "journal_parent": str(self.parent.path),
            "parents": [
                _journal_directory_guard_payload(anchor)
                for _path, anchor in sorted(self._parents_for_payload().items(), key=lambda item: str(item[0]))
            ],
            "records": [_journal_record_payload(record) for record in self.records],
        }

    def _parents_for_payload(self) -> dict[Path, _DirectoryAnchor]:
        return dict(self.anchors)

    def persist(self, *, state: str | None = None) -> None:
        if self.descriptor < 0:
            raise ReleaseError("publication journal descriptor is unavailable")
        if self.durability_uncertain:
            raise ReleaseError(
                "publication journal durability is uncertain; restart recovery is required"
            )
        next_state = self.state if state is None else state
        if next_state not in {"active", "rolling-back", "committed", "rolled-back"}:
            raise ReleaseError("publication journal state is invalid")
        self.parent.assert_current(phase="before journal update")
        named = _validate_regular_at(self.parent, self.name, label="publication journal")
        opened = _file_identity(os.fstat(self.descriptor), label="publication journal")
        if named is None or opened.nlink != 1 or not _same_file_object(named, opened):
            raise ReleaseError("publication journal changed before update")
        self.generation += 1
        self.attempted_state = next_state
        try:
            _write_all(
                self.descriptor,
                _publication_journal_frame(self._payload(state=next_state)),
                label="publication journal",
            )
            _fsync_descriptor(self.descriptor, label="publication journal")
        except BaseException:
            # Do not let a failed terminal-frame write trigger local disposal
            # in ``finally``.  A complete frame might reach storage despite a
            # failing fsync, but this process has no proof of that and must
            # retain every rollback private leaf and the journal name.
            # The append may have completed before a failing fsync.  Never
            # reuse this generation and never infer the terminal outcome
            # from the in-memory attempted state.  A restart parser, bound to
            # the exact journal inode, is the only authority that can decide
            # which complete ordered frame survived.
            self.durability_uncertain = True
            raise
        self.state = next_state
        self.attempted_state = None

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            os.close(descriptor)
        except OSError:
            pass

    def finalize_terminal(self) -> bool:
        """Best-effort private cleanup after a durable terminal frame.

        A post-unlink directory-fsync failure cannot make a caller roll back a
        transaction whose ``committed`` record was already durable.  Until
        unlink succeeds, its exact journal remains recoverable at either its
        original name or an inode-bound terminal disposal name.
        """

        if self.state not in {"committed", "rolled-back"}:
            raise ReleaseError("publication journal is not terminal")
        if self.descriptor < 0:
            return False
        # Do not erase the only durable handle while an open private leaf is
        # still owed cleanup.  In particular, _cleanup_staged_file can move a
        # leaf to a random disposal name and then lose its parent fsync; the
        # live record keeps recovery from calling that unresolved residue a
        # completed transaction.
        if any(
            record.staged is not None
            or record.quarantine is not None
            or record.rollback is not None
            or record.disposal is not None
            or record.staged_name is not None
            or record.staged_identity is not None
            or record.quarantine_name is not None
            or record.quarantine_identity is not None
            or record.rollback_name is not None
            or record.rollback_identity is not None
            or record.disposal_name is not None
            or record.disposal_identity is not None
            or record.disposal_source is not None
            for record in self.records
        ):
            self.close()
            return False
        disposal_name: str | None = None
        current: _FileIdentity | None = None
        try:
            current = _file_identity(os.fstat(self.descriptor), label="publication journal")
            if current.mode != 0o600 or current.nlink != 1:
                raise ReleaseError("publication journal is unsafe during terminal cleanup")
            disposal_name = _terminal_journal_disposal_name(self.name, current)
            staged = _StagedPublicationFile(
                self.parent,
                self.name,
                self.descriptor,
                current,
                "publication journal",
            )
            _cleanup_staged_file(staged, disposal_name=disposal_name)
        except BaseException as exc:
            if disposal_name is not None and current is not None:
                try:
                    _restore_disposed_publication_journal(
                        self.path,
                        expected_identity=current,
                    )
                except BaseException:
                    # A reoccupation or another cleanup interruption leaves
                    # the exact terminal disposal discoverable for restart.
                    pass
            if isinstance(exc, Exception):
                return False
            raise
        finally:
            self.close()
        return True


def _terminal_journal_disposal_name(name: str, identity: _FileIdentity) -> str:
    """Keep journal self-disposal discoverable without weakening ownership."""

    _publication_journal_name(name)
    stem = name[:-len(_PUBLICATION_JOURNAL_SUFFIX)]
    return (
        f"{stem}-dispose-{identity.device}-{identity.inode}-{identity.size}-{identity.mtime_ns}"
        f"{_PUBLICATION_JOURNAL_SUFFIX}"
    )


def _restore_disposed_publication_journal(
    path: Path,
    *,
    expected_identity: _FileIdentity | None = None,
) -> bool:
    """Restore one exact terminal receipt into an absent original name.

    Journal cleanup can be interrupted after its no-replace move.  Its
    discoverable name binds the moved inode, and only a terminal frame with
    no outstanding private obligations permits this name recovery.  A
    concurrently occupied original name always wins the no-replace restore.
    """

    _publication_journal_name(path.name)
    if not _path_lexists(path.parent):
        return False
    parent = _open_directory_anchor(
        path.parent,
        label="publication journal disposal parent",
        create_parents=False,
    )
    try:
        parent.assert_current(phase="before journal disposal recovery")
        try:
            _lstat_at(parent.descriptor, path.name, label="publication journal")
        except FileNotFoundError:
            pass
        else:
            return False
        stem = path.name[:-len(_PUBLICATION_JOURNAL_SUFFIX)]
        pattern = re.compile(
            re.escape(stem) + r"-dispose-([0-9]+)-([0-9]+)-([0-9]+)-(-?[0-9]+)"
            + re.escape(_PUBLICATION_JOURNAL_SUFFIX)
        )
        candidates: list[tuple[str, _AnchoredFileSnapshot]] = []
        for name in os.listdir(parent.descriptor):
            match = pattern.fullmatch(name)
            if match is None:
                continue
            named = _validate_regular_at(parent, name, label="terminal journal disposal")
            if named is None:
                continue
            if name != _terminal_journal_disposal_name(path.name, named):
                continue
            if expected_identity is not None and not _same_renamed_file_identity(
                named, expected_identity,
            ):
                continue
            journal_read = _read_publication_journal(
                path.parent / name,
                recover_disposal=False,
            )
            if journal_read is None:
                continue
            _candidate_path, capture, payload = journal_read
            if capture.identity != named:
                raise ReleaseError("terminal journal disposal changed during recovery")
            guards = _journal_directory_guards_from_payload(payload["parents"])
            if guards.for_path(path) != parent.guard:
                raise ReleaseError("terminal journal disposal parent changed")
            records = [
                _journal_record_from_payload(value, guards=guards)
                for value in payload["records"]
            ]
            if payload["state"] not in {"committed", "rolled-back"} or any(
                record.staged_name is not None
                or record.quarantine_name is not None
                or record.rollback_name is not None
                or record.disposal_name is not None
                for record in records
            ):
                raise ReleaseError("journal disposal is not terminally clean")
            candidates.append((name, capture))
        if not candidates:
            return False
        if len(candidates) != 1:
            raise ReleaseError("multiple terminal journal disposals require exact recovery")
        name, capture = candidates[0]
        _restore_quarantined_leaf(
            parent,
            name,
            path.name,
            expected=capture,
            label="terminal publication journal",
        )
        return True
    finally:
        parent.close()


class _PublicationJournalCreateError(ReleaseError):
    """Creation retained a parseable journal instead of claiming local cleanup."""

    def __init__(
        self,
        journal: _PublicationJournal,
        cause: BaseException,
        *,
        original_cause: BaseException | None = None,
    ) -> None:
        super().__init__(f"publication journal creation durability failed: {cause}")
        self.journal = journal
        self.cause = cause
        # A parent-fsync error can occur while preserving an earlier
        # KeyboardInterrupt from fchmod/fstat/validation.  The outer
        # transaction must retain its recovery handle and still rethrow that
        # initiating BaseException.
        self.original_cause = original_cause if original_cause is not None else cause


class _PublicationJournalPreflightError(ReleaseError):
    """Journal setup stopped before any staged or canonical mutation.

    This deliberately avoids the generic transaction's ``was rolled back``
    wording.  There was no publication to undo, and the original setup error
    may have left a foreign journal-name winner that must remain untouched.
    """

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.cause = cause


def _publication_transaction_receipt(
    journal: _PublicationJournal,
    parents: _PublicationParents,
    records: Sequence[_PublicationRecord],
    *,
    state: str,
    postimages: Mapping[Path, _AnchoredFileSnapshot] | None = None,
) -> PublicationTransactionReceipt:
    """Freeze the exact ownership material handed to a durable callback."""

    if state not in {"intent", "published", "recovered"}:
        raise ReleaseError("publication receipt state is invalid")
    parents.assert_current(phase=f"before {state} publication receipt")
    journal_identity = _file_identity(
        os.fstat(journal.descriptor),
        label="publication journal",
    )
    if journal_identity.nlink != 1:
        raise ReleaseError("publication journal is unsafe for callback receipt")
    named = _validate_regular_at(journal.parent, journal.name, label="publication journal")
    if named is None or not _same_file_object(named, journal_identity):
        raise ReleaseError("publication journal changed before callback receipt")
    preimages = {record.path: record.snapshot for record in records}
    return PublicationTransactionReceipt(
        journal_path=journal.path,
        journal_identity=journal_identity,
        state=state,
        parent_guards=_PublicationParentGuards(
            tuple(anchor.guard for _path, anchor in sorted(parents.anchors().items(), key=lambda item: str(item[0])))
        ),
        preimages=MappingProxyType(preimages),
        postimages=MappingProxyType(dict(postimages or {})),
    )


def _capture_published_postimages(
    parents: _PublicationParents,
    records: Sequence[_PublicationRecord],
) -> Mapping[Path, _AnchoredFileSnapshot]:
    """Capture every current B identity before the journal becomes terminal."""

    result: dict[Path, _AnchoredFileSnapshot] = {}
    for record in records:
        parent = parents.for_path(record.path)
        observed = _capture_anchored_file(parent, record.name, label=record.label)
        if record.content is None:
            if observed.snapshot.exists:
                raise ReleaseError(f"{record.label} reappeared before publication receipt")
            result[record.path] = observed
            continue
        if record.committed_identity is None:
            raise ReleaseError(f"{record.label} has no authenticated postimage")
        expected = _staged_anchored_snapshot(record, record.committed_identity)
        if not _anchored_snapshot_matches_after_rename(observed, expected):
            raise ReleaseError(f"{record.label} changed before publication receipt")
        result[record.path] = observed
    return MappingProxyType(result)


def _capture_recovered_postimages(
    parents: _PublicationParents,
    records: Sequence[_PublicationRecord],
) -> Mapping[Path, _AnchoredFileSnapshot]:
    """Capture exact canonical leaves after a journaled inverse completes.

    A recovery may restore an original tombstone (whose preimage inode is
    still authoritative), recreate a lost preimage from durable bytes (whose
    new inode is journaled), or remove an explicitly absent preimage.  This
    function never treats matching bytes as authority: each present leaf must
    match one of those exact durable identities before it can be handed to an
    outer transaction for strict adoption.
    """

    result: dict[Path, _AnchoredFileSnapshot] = {}
    for record in records:
        parent = parents.for_path(record.path)
        observed = _capture_anchored_file(parent, record.name, label=record.label)
        expected = record.snapshot
        if not expected.snapshot.exists:
            if observed.snapshot.exists:
                raise ReleaseError(
                    f"{record.label} reappeared before recovered postimage receipt"
                )
            result[record.path] = observed
            continue
        expected_digest = expected.sha256 or sha256_bytes(expected.snapshot.content)
        if observed.snapshot != expected.snapshot or observed.sha256 != expected_digest:
            raise ReleaseError(
                f"{record.label} does not match its durable recovered preimage"
            )
        matches_original = _anchored_snapshot_matches_after_rename(observed, expected)
        matches_recreated = False
        if record.committed_identity is not None:
            recreated = _AnchoredFileSnapshot(
                expected.snapshot,
                record.committed_identity,
                expected_digest,
            )
            matches_recreated = _anchored_snapshot_matches_after_rename(observed, recreated)
        if not matches_original and not matches_recreated:
            raise ReleaseError(
                f"{record.label} changed after restart recovery before exact receipt"
            )
        result[record.path] = observed
    return MappingProxyType(result)


def _publication_receipt_summary(
    receipt: PublicationTransactionReceipt,
) -> dict[str, object]:
    """Return the stable JSON-safe portion of a callback receipt."""

    def snapshot_item(path: Path, item: _AnchoredFileSnapshot) -> dict[str, object]:
        return {
            "path": str(path),
            "exists": item.snapshot.exists,
            "mode": item.snapshot.mode,
            "sha256": item.sha256,
            "identity": _journal_file_identity_payload(item.identity),
        }

    return {
        "journal_path": str(receipt.journal_path),
        "journal_identity": _journal_file_identity_payload(receipt.journal_identity),
        "state": receipt.state,
        "parents": [
            {
                "path": str(guard.path),
                "chain": [
                    {"device": item.device, "inode": item.inode, "mode": item.mode}
                    for item in guard.chain
                ],
            }
            for guard in receipt.parent_guards.guards
        ],
        "preimages": [
            snapshot_item(path, item)
            for path, item in sorted(receipt.preimages.items(), key=lambda pair: str(pair[0]))
        ],
        "postimages": [
            snapshot_item(path, item)
            for path, item in sorted(receipt.postimages.items(), key=lambda pair: str(pair[0]))
        ],
    }


def _journal_record_phase(
    journal: _PublicationJournal | None,
    record: _PublicationRecord,
    phase: str,
) -> None:
    record.phase = phase
    if journal is not None:
        journal.persist()


def _unsupported_publication_operation(operation: str, exc: BaseException) -> ReleaseError:
    return ReleaseError(
        f"secure descriptor-relative {operation} is unavailable on this platform"
    )


def _canonical_publication_path(path: Path | str | os.PathLike[str], *, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ReleaseError(f"{label} is not a path: {path!r}") from exc
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if not raw or not os.path.isabs(raw) or raw != os.path.normpath(raw):
        raise ReleaseError(f"{label} is not an absolute canonical path: {raw!r}")
    candidate = Path(raw)
    if any(part in (".", "..") for part in candidate.parts):
        raise ReleaseError(f"{label} is not an absolute canonical path: {raw!r}")
    return candidate


def _directory_identity(info: os.stat_result, *, label: str) -> _DirectoryIdentity:
    try:
        device = int(info.st_dev)
        inode = int(info.st_ino)
        mode = int(info.st_mode)
        nlink = int(info.st_nlink)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseError(f"filesystem identity is unsupported for {label}") from exc
    if device < 0 or inode <= 0 or nlink <= 0 or not stat.S_ISDIR(mode):
        raise ReleaseError(f"filesystem identity is unsupported for {label}")
    return _DirectoryIdentity(device, inode, stat.S_IMODE(mode))


def _file_identity(info: os.stat_result, *, label: str) -> _FileIdentity:
    try:
        device = int(info.st_dev)
        inode = int(info.st_ino)
        mode = int(info.st_mode)
        nlink = int(info.st_nlink)
        size = int(info.st_size)
        mtime_ns = int(info.st_mtime_ns)
        ctime_ns = int(info.st_ctime_ns)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseError(f"filesystem identity is unsupported for {label}") from exc
    if device < 0 or inode <= 0 or nlink <= 0:
        raise ReleaseError(f"filesystem identity is unsupported for {label}")
    return _FileIdentity(
        device,
        inode,
        stat.S_IMODE(mode),
        nlink,
        size,
        mtime_ns,
        ctime_ns,
    )


def _same_file_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and left.mode == right.mode
        and left.nlink == right.nlink
    )


def _same_created_journal_leaf(left: _FileIdentity, right: _FileIdentity) -> bool:
    """Compare the immutable ownership facts of a just-created journal leaf.

    An attempted fchmod legitimately changes mode and ctime before the setup
    code sees its result.  The cleanup proof therefore binds the original
    O_EXCL object by device/inode and requires both views to remain
    single-linked; it never treats a later same-name inode as ours.
    """

    return (
        left.device == right.device
        and left.inode == right.inode
        and left.nlink == 1
        and right.nlink == 1
    )


def _safe_component(name: str, *, label: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ReleaseError(f"unsafe publication file name for {label}: {name!r}")
    return name


def _nofollow_open_flag() -> int:
    """Return O_NOFOLLOW, refusing platforms which cannot prove the walk."""

    flag = getattr(os, "O_NOFOLLOW", 0)
    if isinstance(flag, bool) or not isinstance(flag, int) or flag == 0:
        raise ReleaseError(
            "secure descriptor-relative publication requires O_NOFOLLOW"
        )
    return flag


def _directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    if isinstance(directory, bool) or not isinstance(directory, int) or directory == 0:
        raise ReleaseError(
            "secure descriptor-relative publication requires O_DIRECTORY"
        )
    return (
        os.O_RDONLY
        | directory
        | getattr(os, "O_CLOEXEC", 0)
        | _nofollow_open_flag()
    )


def _file_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _nofollow_open_flag()


def _lstat_at(descriptor: int, name: str, *, label: str) -> os.stat_result:
    _safe_component(name, label=label)
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except (TypeError, NotImplementedError) as exc:
        raise _unsupported_publication_operation("stat", exc) from exc


def _openat(
    descriptor: int,
    name: str,
    flags: int,
    *,
    mode: int = 0o600,
    label: str,
) -> int:
    _safe_component(name, label=label)
    try:
        return os.open(name, flags, mode, dir_fd=descriptor)
    except (TypeError, NotImplementedError) as exc:
        raise _unsupported_publication_operation("open", exc) from exc


def _mkdirat(descriptor: int, name: str, *, mode: int, label: str) -> None:
    _safe_component(name, label=label)
    try:
        os.mkdir(name, mode=mode, dir_fd=descriptor)
    except (TypeError, NotImplementedError) as exc:
        raise _unsupported_publication_operation("mkdir", exc) from exc


def _renameat2(
    descriptor: int,
    source: str,
    destination: str,
    *,
    flags: int,
    label: str,
) -> None:
    """Atomically move two entries beneath one validated directory FD.

    Linux has no conditional unlink.  ``RENAME_NOREPLACE`` and
    ``RENAME_EXCHANGE`` are therefore the leaf-level compare-and-swap
    primitives for this transaction.  Do not silently fall back to
    ``os.replace``: that fallback would reintroduce the stale-name overwrite
    this helper exists to prevent.
    """

    _safe_component(source, label=label)
    _safe_component(destination, label=label)
    if flags not in {_RENAME_NOREPLACE, _RENAME_EXCHANGE}:
        raise ReleaseError(f"unsupported secure rename flags for {label}")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise ReleaseError(
            "secure descriptor-relative publication requires "
            "renameat2(RENAME_NOREPLACE/RENAME_EXCHANGE)"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        result = renameat2(
            descriptor,
            os.fsencode(source),
            descriptor,
            os.fsencode(destination),
            flags,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error != errno.EINTR:
            break
        if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
            raise ReleaseError(
                f"secure descriptor-relative rename interrupted "
                f"{_PUBLICATION_EINTR_RETRIES} times for {label}"
            )
    else:  # pragma: no cover - the bounded branch returns or raises above
        raise ReleaseError(f"secure descriptor-relative rename was interrupted for {label}")
    if error == errno.ENOENT:
        raise FileNotFoundError(error, os.strerror(error), source)
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    if error in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.ENOTSUP,
    }:
        raise ReleaseError(
            "secure descriptor-relative publication requires "
            "renameat2(RENAME_NOREPLACE/RENAME_EXCHANGE)"
        )
    raise OSError(error, os.strerror(error), source, destination)


def _unlinkat(descriptor: int, name: str, *, label: str) -> None:
    _safe_component(name, label=label)
    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        try:
            os.unlink(name, dir_fd=descriptor)
            return
        except (TypeError, NotImplementedError) as exc:
            raise _unsupported_publication_operation("unlink", exc) from exc
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise
        if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
            break
    raise ReleaseError(
        f"could not unlink {label}: interrupted {_PUBLICATION_EINTR_RETRIES} times"
    )


def _open_directory_anchor(
    path: Path | str | os.PathLike[str],
    *,
    label: str,
    create_parents: bool,
) -> _DirectoryAnchor:
    """Walk an absolute directory path with no-follow, descriptor-relative opens."""

    checked = _canonical_publication_path(path, label=label)
    try:
        current = os.open("/", _directory_open_flags())
    except OSError as exc:
        raise ReleaseError(f"could not open publication filesystem root: {exc}") from exc
    try:
        chain: list[_DirectoryIdentity] = [
            _directory_identity(os.fstat(current), label="publication filesystem root")
        ]
        prefix = Path("/")
        for component in checked.parts[1:]:
            _safe_component(component, label=label)
            try:
                before = _lstat_at(current, component, label=label)
            except FileNotFoundError:
                if not create_parents:
                    raise ReleaseError(f"publication parent does not exist: {checked}") from None
                # A descriptor alone keeps mutations in the original tree;
                # this fresh identity comparison also rejects a replaced
                # named ancestor before creating a new directory.
                _assert_directory_prefix_current(prefix, tuple(chain), label=label)
                try:
                    _mkdirat(current, component, mode=0o700, label=label)
                except FileExistsError:
                    pass
                else:
                    _fsync_descriptor(
                        current,
                        label=f"created publication parent: {prefix}",
                    )
                before = _lstat_at(current, component, label=label)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ReleaseError(f"publication parent is not a real directory: {checked}")
            child = _openat(current, component, _directory_open_flags(), label=label)
            try:
                after = _directory_identity(os.fstat(child), label=label)
            except Exception:
                os.close(child)
                raise
            observed = _directory_identity(before, label=label)
            if observed != after:
                os.close(child)
                raise ReleaseError(f"publication parent changed while opening: {checked}")
            os.close(current)
            current = child
            chain.append(after)
            prefix = prefix / component
        return _DirectoryAnchor(checked, current, tuple(chain), label)
    except Exception:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _assert_directory_prefix_current(
    path: Path,
    expected: tuple[_DirectoryIdentity, ...],
    *,
    label: str,
) -> None:
    fresh = _open_directory_anchor(path, label=label, create_parents=False)
    try:
        if fresh.chain != expected:
            raise ReleaseError(f"publication parent identity changed before directory creation: {path}")
    finally:
        fresh.close()


def _coerce_parent_guards(value: object | None) -> _PublicationParentGuards | None:
    if value is None:
        return None
    if not isinstance(value, _PublicationParentGuards):
        raise ReleaseError("publication parent identities are unsupported")
    return value


def _capture_publication_parent_guards(
    paths: Sequence[Path | str | os.PathLike[str]],
    *,
    create_parents: bool = False,
) -> _PublicationParentGuards:
    """Capture every output parent and every ancestor as a reusable guard."""

    normalized = [_canonical_publication_path(path, label="publication path") for path in paths]
    if not normalized:
        raise ReleaseError("publication has no paths to anchor")
    guards: list[_DirectoryGuard] = []
    for parent in sorted({path.parent for path in normalized}, key=str):
        anchor = _open_directory_anchor(
            parent,
            label=f"publication parent {parent}",
            create_parents=create_parents,
        )
        try:
            anchor.assert_current(phase="during preflight")
            guards.append(anchor.guard)
        finally:
            anchor.close()
    return _PublicationParentGuards(tuple(guards))


def _open_publication_parents(
    paths: Sequence[Path],
    *,
    parent_guards: object | None = None,
    create_parents: bool = False,
) -> _PublicationParents:
    expected = _coerce_parent_guards(parent_guards)
    anchors: dict[Path, _DirectoryAnchor] = {}
    try:
        for parent in sorted({path.parent for path in paths}, key=str):
            anchor = _open_directory_anchor(
                parent,
                label=f"publication parent {parent}",
                create_parents=create_parents,
            )
            if expected is not None:
                guard = expected.for_path(parent / ".publication-anchor")
                if guard is None or guard != anchor.guard:
                    anchor.close()
                    raise ReleaseError(f"publication parent identity changed before mutation: {parent}")
            anchors[parent] = anchor
        result = _PublicationParents(anchors)
        result.assert_current(phase="before mutation")
        return result
    except Exception:
        for anchor in anchors.values():
            anchor.close()
        raise


def _validate_regular_at(
    parent: _DirectoryAnchor,
    name: str,
    *,
    label: str,
) -> _FileIdentity | None:
    try:
        info = _lstat_at(parent.descriptor, name, label=label)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"{label} is not a regular file")
    identity = _file_identity(info, label=label)
    if identity.nlink != 1:
        raise ReleaseError(f"{label} has unexpected hard links")
    return identity


def _read_regular_at(
    parent: _DirectoryAnchor,
    name: str,
    *,
    label: str,
) -> tuple[bytes, _FileIdentity]:
    before = _validate_regular_at(parent, name, label=label)
    if before is None:
        raise FileNotFoundError(name)
    descriptor = _openat(parent.descriptor, name, _file_open_flags(), label=label)
    try:
        opened = _file_identity(os.fstat(descriptor), label=label)
        if opened.nlink != 1 or before != opened:
            raise ReleaseError(f"{label} changed while opening")
        chunks: list[bytes] = []
        while True:
            try:
                block = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not block:
                break
            chunks.append(block)
        after = _file_identity(os.fstat(descriptor), label=label)
        if after.nlink != 1 or after != opened:
            raise ReleaseError(f"{label} changed while reading")
        # The opened inode alone does not prove that the public name still
        # selects these bytes.  Retain the descriptor until both observations
        # agree, including metadata changes made in place during capture.
        named = _validate_regular_at(parent, name, label=label)
        if named != after:
            raise ReleaseError(f"{label} changed after reading")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _capture_anchored_file(
    parent: _DirectoryAnchor,
    name: str,
    *,
    label: str,
) -> _AnchoredFileSnapshot:
    identity = _validate_regular_at(parent, name, label=label)
    if identity is None:
        return _AnchoredFileSnapshot(_FileSnapshot(False), None, None)
    content, observed = _read_regular_at(parent, name, label=label)
    return _AnchoredFileSnapshot(
        _FileSnapshot(True, content, observed.mode),
        observed,
        sha256_bytes(content),
    )


def _capture_anchored_files_safely(
    paths: Sequence[tuple[Path, str]],
    *,
    parent_guards: object | None = None,
    create_parents: bool = False,
) -> dict[Path, _AnchoredFileSnapshot]:
    """Capture file bytes and exact identities through validated parent FDs."""

    normalized = [(_canonical_publication_path(path, label=label), label) for path, label in paths]
    parents = _open_publication_parents(
        [path for path, _label in normalized],
        parent_guards=parent_guards,
        create_parents=create_parents,
    )
    try:
        parents.assert_current(phase="before preimage capture")
        result: dict[Path, _AnchoredFileSnapshot] = {}
        for path, label in normalized:
            parents.assert_current(phase="during preimage capture")
            parent = parents.for_path(path)
            result[path] = _capture_anchored_file(parent, path.name, label=label)
        parents.assert_current(phase="after preimage capture")
        for path, label in normalized:
            observed = _validate_regular_at(parents.for_path(path), path.name, label=label)
            if observed != result[path].identity:
                raise ReleaseError(f"{label} changed after preimage capture")
        return result
    finally:
        parents.close()


def _capture_files_safely(
    paths: Sequence[tuple[Path, str]],
    *,
    parent_guards: object | None = None,
    create_parents: bool = False,
) -> dict[Path, _FileSnapshot]:
    """Capture regular-file preimages through validated parent descriptors."""

    anchored = _capture_anchored_files_safely(
        paths,
        parent_guards=parent_guards,
        create_parents=create_parents,
    )
    return {path: item.snapshot for path, item in anchored.items()}


def _fsync_directory(path: Path) -> None:
    anchor = _open_directory_anchor(path, label=f"directory {path}", create_parents=False)
    try:
        anchor.assert_current(phase="before fsync")
        anchor.fsync(label="directory")
        anchor.assert_current(phase="after fsync")
    finally:
        anchor.close()


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _snapshot_from_object(value: object, *, label: str) -> _FileSnapshot:
    exists = getattr(value, "exists", None)
    content = getattr(value, "content", None)
    mode = getattr(value, "mode", None)
    if isinstance(value, Mapping):
        required = {"exists", "content", "mode"}
        if not required.issubset(value):
            raise ReleaseError(f"{label} snapshot is malformed")
        exists = value["exists"]
        content = value["content"]
        mode = value["mode"]
    if not isinstance(exists, bool) or not isinstance(content, bytes):
        raise ReleaseError(f"{label} snapshot is malformed")
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
        raise ReleaseError(f"{label} snapshot mode is malformed")
    if not exists and content:
        raise ReleaseError(f"{label} absent snapshot contains bytes")
    return _FileSnapshot(exists, content, mode)


_MISSING_SNAPSHOT_FIELD = object()


def _snapshot_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING_SNAPSHOT_FIELD)
    return getattr(value, name, _MISSING_SNAPSHOT_FIELD)


def _anchored_identity_from_object(
    value: object,
    snapshot: _FileSnapshot,
    *,
    label: str,
) -> tuple[_FileIdentity | None, str | None]:
    """Recover a complete anchor emitted by ``service_wrappers``.

    Older, explicitly content-only callers remain usable, but a partial
    identity is never guessed.  A captured wrapper snapshot carries every
    field below, so losing any one of them is an authentication failure rather
    than an invitation to compare only bytes and mode.
    """

    names = ("device", "inode", "nlink", "size", "mtime_ns", "ctime_ns", "sha256")
    values = {name: _snapshot_field(value, name) for name in names}
    present = {name: item is not _MISSING_SNAPSHOT_FIELD for name, item in values.items()}
    if not any(present.values()):
        return None, None
    if not all(present.values()):
        raise ReleaseError(f"{label} snapshot identity is incomplete")
    if snapshot.exists and all(values[name] is None for name in names):
        # A manually constructed legacy FileSnapshot exposes dataclass fields
        # with their ``None`` defaults.  Treat that explicit all-None form as
        # content-only, never as a partly trusted identity.
        return None, None
    if not snapshot.exists:
        if any(values[name] is not None for name in names):
            raise ReleaseError(f"{label} absent snapshot has an identity")
        return None, None

    for name in ("device", "inode", "nlink", "size", "mtime_ns", "ctime_ns"):
        item = values[name]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ReleaseError(f"{label} snapshot {name} is malformed")
    device = int(values["device"])
    inode = int(values["inode"])
    nlink = int(values["nlink"])
    size = int(values["size"])
    mtime_ns = int(values["mtime_ns"])
    ctime_ns = int(values["ctime_ns"])
    digest = values["sha256"]
    if (
        device < 0
        or inode <= 0
        or nlink != 1
        or size < 0
        or not isinstance(digest, str)
        or _RELEASE_ID.fullmatch(digest) is None
        or size != len(snapshot.content)
        or digest != sha256_bytes(snapshot.content)
    ):
        raise ReleaseError(f"{label} snapshot identity is malformed")
    return (
        _FileIdentity(
            device,
            inode,
            snapshot.mode,
            nlink,
            size,
            mtime_ns,
            ctime_ns,
        ),
        digest,
    )


def _expected_anchored_snapshot_from_object(
    value: object,
    *,
    label: str,
) -> _AnchoredFileSnapshot:
    """Normalize a preimage, retaining an identity when the caller has one."""

    if isinstance(value, _AnchoredFileSnapshot):
        return value
    snapshot = _snapshot_from_object(value, label=label)
    identity, digest = _anchored_identity_from_object(value, snapshot, label=label)
    return _AnchoredFileSnapshot(snapshot, identity, digest)


def _capture_file(
    path: Path,
    *,
    label: str,
    parent_guards: object | None = None,
) -> _FileSnapshot:
    return _capture_files_safely(
        ((_canonical_publication_path(path, label=label), label),),
        parent_guards=parent_guards,
    )[_canonical_publication_path(path, label=label)]


def _write_all(descriptor: int, content: bytes, *, label: str) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        except OSError as exc:
            raise ReleaseError(f"could not write {label}: {exc}") from exc
        if written <= 0:
            raise ReleaseError(f"could not write {label}: short write")
        view = view[written:]


def _stage_publication_file(
    parent: _DirectoryAnchor,
    name: str,
    content: bytes,
    *,
    mode: int,
    label: str,
    temporary_name: str | None = None,
    record: _PublicationRecord | None = None,
    journal: _PublicationJournal | None = None,
) -> _StagedPublicationFile:
    """Write and fsync an unlinked-from-publication temporary through a dir FD."""

    parent.assert_current(phase="before staging")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | _nofollow_open_flag()
    )
    supplied_name = temporary_name
    for _attempt in range(32):
        temporary_name = supplied_name or _new_private_publication_name(name, purpose="tmp")
        _safe_component(temporary_name, label=f"{label} temporary")
        try:
            descriptor = _openat(
                parent.descriptor,
                temporary_name,
                flags,
                mode=mode,
                label=label,
            )
        except FileExistsError:
            if supplied_name is not None:
                raise ReleaseError(f"{label} journal temporary name is already occupied") from None
            continue
        initial: _FileIdentity | None = None
        staged: _StagedPublicationFile | None = None
        try:
            initial = _file_identity(os.fstat(descriptor), label=label)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or initial.nlink != 1:
                raise ReleaseError(f"{label} temporary file is unsafe")
            try:
                os.fchmod(descriptor, mode)
            except (AttributeError, OSError) as exc:
                raise ReleaseError(f"could not set mode for {label}: {exc}") from exc
            _write_all(descriptor, content, label=label)
            _fsync_descriptor(descriptor, label=label)
            final = _file_identity(os.fstat(descriptor), label=label)
            named = _validate_regular_at(parent, temporary_name, label=f"{label} temporary")
            if (
                final.nlink != 1
                or named is None
                or named.nlink != 1
                or not _same_file_object(final, named)
            ):
                raise ReleaseError(f"{label} temporary file changed before commit")
            staged = _StagedPublicationFile(parent, temporary_name, descriptor, final, label)
            if record is not None:
                if record.staged_name != temporary_name:
                    raise ReleaseError(f"{label} staged name does not match its journal intent")
                # Persist the full post-stage identity before returning to the
                # caller.  This closes the ordinary Python signal window
                # between a successful stage and the caller's assignment.
                record.staged = staged
                record.staged_identity = staged.identity
                _journal_record_phase(journal, record, "staged")
            return staged
        except BaseException:
            if record is not None and staged is not None and record.staged is staged:
                # The outer transaction owns this still-open descriptor and
                # will either clean the exact staged leaf or retain its
                # journal.  Closing it here would turn a failed cleanup into
                # an untracked private residue.
                raise
            if initial is not None:
                try:
                    _cleanup_staged_file(
                        staged
                        or _StagedPublicationFile(
                            parent,
                            temporary_name,
                            descriptor,
                            initial,
                            label,
                        )
                    )
                except Exception:
                    pass
            if staged is not None:
                staged.close()
            else:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
    raise ReleaseError(f"could not allocate a temporary file for {label}")


def _cleanup_staged_file(
    staged: _StagedPublicationFile,
    *,
    disposal_name: str | None = None,
    after_dispose: Callable[[], None] | None = None,
) -> None:
    """Remove a private temporary without unlinking its public-style name.

    ``unlinkat`` cannot compare an inode.  Move the checked temporary to a
    fresh, no-replace disposal name first, so a leaf swapped into the original
    temporary name is never unlinked by this transaction.  The final unlink is
    intentionally limited to that random private name; see the transaction
    docstring for the remaining kernel-level uncoordinated-writer boundary.
    """

    if staged.descriptor < 0:
        return
    staged.parent.assert_current(phase="before temporary cleanup")
    current = _validate_regular_at(
        staged.parent,
        staged.temporary_name,
        label=f"{staged.label} temporary",
    )
    if current is None:
        return
    if (
        current.nlink != 1
        or current != staged.identity
    ):
        raise ReleaseError(f"{staged.label} temporary file changed during cleanup")
    supplied_disposal_name = disposal_name
    disposal_name = supplied_disposal_name or _new_private_publication_name(
        staged.temporary_name,
        purpose="dispose",
    )
    for _attempt in range(32):
        try:
            staged.parent.assert_current(phase="immediately before temporary cleanup")
            _renameat2(
                staged.parent.descriptor,
                staged.temporary_name,
                disposal_name,
                flags=_RENAME_NOREPLACE,
                label=f"{staged.label} temporary cleanup",
            )
            break
        except FileExistsError:
            if supplied_disposal_name is not None:
                raise ReleaseError(
                    f"{staged.label} registered disposal name is already occupied"
                ) from None
            disposal_name = _new_private_publication_name(
                staged.temporary_name,
                purpose="dispose",
            )
        except FileNotFoundError:
            # A concurrent remover won the race.  It did not make us unlink
            # a replacement, so leave the transaction fail-closed only when
            # the caller still needs this temporary for recovery.
            return
    else:  # pragma: no cover - UUID collisions are not realistically reachable
        raise ReleaseError(f"could not allocate a disposal name for {staged.label}")
    moved = _validate_regular_at(
        staged.parent,
        disposal_name,
        label=f"{staged.label} disposal",
    )
    if (
        moved is None
        or moved.nlink != 1
        or not _same_renamed_file_identity(moved, staged.identity)
    ):
        # The public temporary name is now absent, so this no-replace move can
        # restore whatever was moved without overwriting an intervening leaf.
        try:
            _renameat2(
                staged.parent.descriptor,
                disposal_name,
                staged.temporary_name,
                flags=_RENAME_NOREPLACE,
                label=f"{staged.label} temporary cleanup restore",
            )
            staged.parent.fsync(label="publication parent")
        except Exception as exc:
            raise ReleaseError(
                f"{staged.label} temporary changed during cleanup and could not be restored: {exc}"
            ) from exc
        raise ReleaseError(f"{staged.label} temporary file changed during cleanup")
    if after_dispose is not None:
        # The source name is now absent.  Persisting the exact disposal name
        # before unlink makes an interruption here resumable without ever
        # guessing whether a new private leaf belongs to this transaction.
        after_dispose()
    _unlinkat(staged.parent.descriptor, disposal_name, label=staged.label)
    staged.parent.fsync(label="publication parent")


def _dispose_record_private(
    record: _PublicationRecord,
    staged: _StagedPublicationFile,
    *,
    source: str,
    journal: _PublicationJournal | None,
) -> None:
    """Durably remove a transaction-owned private leaf through a disposal name."""

    if source not in {"staged", "quarantine", "rollback"}:
        raise ReleaseError("publication private disposal source is invalid")
    if journal is None:
        _cleanup_staged_file(staged)
        return
    # The disposal intent is durable before the first move.  Do not reuse a
    # partially attempted name: another writer could have occupied it after a
    # failed no-replace attempt, so make a fresh intent in that case.
    for _attempt in range(32):
        record.disposal_name = _new_private_publication_name(
            staged.temporary_name,
            purpose="dispose",
        )
        record.disposal_identity = staged.identity
        record.disposal_source = source
        _journal_record_phase(journal, record, "cleanup-intent")
        try:
            _cleanup_staged_file(
                staged,
                disposal_name=record.disposal_name,
                after_dispose=lambda: _journal_record_phase(
                    journal,
                    record,
                    "cleanup-disposed",
                ),
            )
            break
        except ReleaseError as exc:
            if "registered disposal name is already occupied" not in str(exc):
                raise
    else:  # pragma: no cover - UUID collisions are not realistically reachable
        raise ReleaseError(f"could not allocate a disposal name for {record.label}")
    record.disposal_name = None
    record.disposal_identity = None
    record.disposal_source = None
    _journal_record_phase(journal, record, "cleanup-complete")


def _new_private_publication_name(name: str, *, purpose: str) -> str:
    _safe_component(name, label="publication temporary")
    _safe_component(purpose, label="publication temporary purpose")
    return f".{name}.{purpose}-{os.getpid()}-{uuid.uuid4().hex}"


def _same_renamed_file_identity(
    left: _FileIdentity,
    right: _FileIdentity,
) -> bool:
    """Compare fields a directory-entry rename cannot legitimately change.

    A rename updates ``ctime`` on Linux, but it must not update ``mtime``.
    Keeping ``mtime`` in this comparison means the ctime concession does not
    accidentally turn an in-place write into a content-only comparison.
    """

    return (
        left.device == right.device
        and left.inode == right.inode
        and left.mode == right.mode
        and left.nlink == right.nlink
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
    )


def _anchored_snapshot_matches_after_rename(
    observed: _AnchoredFileSnapshot,
    expected: _AnchoredFileSnapshot,
) -> bool:
    """Validate a renamed leaf while allowing its rename-updated ctime."""

    if observed.snapshot != expected.snapshot:
        return False
    expected_digest = expected.sha256
    if expected_digest is None and expected.snapshot.exists:
        expected_digest = sha256_bytes(expected.snapshot.content)
    if observed.sha256 != expected_digest:
        return False
    if expected.identity is None:
        return observed.identity is None
    return (
        observed.identity is not None
        and _same_renamed_file_identity(observed.identity, expected.identity)
    )


def _staged_anchored_snapshot(
    record: _PublicationRecord,
    identity: _FileIdentity,
) -> _AnchoredFileSnapshot:
    if record.content is None:  # pragma: no cover - only called for writes
        raise ReleaseError(f"{record.label} has no staged bytes")
    return _AnchoredFileSnapshot(
        _FileSnapshot(True, record.content, record.mode),
        identity,
        sha256_bytes(record.content),
    )


def _adopt_interrupted_staged_publication(
    record: _PublicationRecord,
    staged: _StagedPublicationFile,
    *,
    journal: _PublicationJournal | None,
) -> None:
    """Record an exact B that a signal may have interrupted after rename.

    ``renameat2`` can complete before Python receives a signal raised by a
    later capture, fsync, or journal write.  The durable ``publish-intent``
    frame already binds the staged inode; this best-effort adoption gives the
    in-process BaseException rollback the same proof.  Failure to persist the
    follow-up frame is safe: restart recovery still has the staged identity
    from the preceding frame.
    """

    try:
        observed = _capture_anchored_file(
            staged.parent,
            record.name,
            label=record.label,
        )
        expected = _staged_anchored_snapshot(record, staged.identity)
        if not _anchored_snapshot_matches_after_rename(observed, expected):
            return
        if observed.identity is None:  # pragma: no cover - exact staged B exists
            return
        record.committed_identity = observed.identity
        try:
            staged.parent.fsync(label="publication parent")
            _journal_record_phase(journal, record, "published")
        except BaseException:
            # ``publish-intent`` + the durable staged identity remains the
            # restart-safe source of truth when the follow-up write is cut
            # short.  Preserve the original interruption for the caller.
            pass
        # This descriptor still refers to B but its temporary name was moved
        # to the canonical destination.  Leaving it in ``record.staged``
        # would make terminal finalization retain a non-existent temporary
        # forever after a signal between rename and normal cleanup.
        if record.staged is staged:
            staged.close()
            record.staged = None
            record.staged_name = None
            record.staged_identity = None
            # Keep the journal's private-name vocabulary accurate even on
            # the BaseException path.  If this append is uncertain the outer
            # transaction retains the prior frame and asks restart recovery
            # to resolve it rather than terminally deleting the journal.
            _journal_record_phase(journal, record, "published")
    except BaseException:
        # The caller is already handling an interruption.  Do not replace it
        # with a best-effort adoption failure; the durable intent frame still
        # causes restart recovery to fail closed or recover the exact B.
        pass


def _open_named_staged_file(
    parent: _DirectoryAnchor,
    name: str,
    *,
    label: str,
) -> _StagedPublicationFile:
    """Open one already-named private leaf and bind it to its FD identity."""

    named = _validate_regular_at(parent, name, label=label)
    if named is None:
        raise ReleaseError(f"{label} disappeared")
    descriptor = _openat(parent.descriptor, name, _file_open_flags(), label=label)
    try:
        opened = _file_identity(os.fstat(descriptor), label=label)
        if (
            opened.nlink != 1
            or not _same_file_object(named, opened)
            or opened.size != named.size
        ):
            raise ReleaseError(f"{label} changed while opening")
        return _StagedPublicationFile(parent, name, descriptor, opened, label)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _restore_quarantined_leaf(
    parent: _DirectoryAnchor,
    quarantine_name: str,
    public_name: str,
    *,
    expected: _AnchoredFileSnapshot,
    label: str,
) -> None:
    """Move a displaced leaf back only into an atomically empty public name."""

    parent.assert_current(phase="before quarantine restore")
    observed = _capture_anchored_file(parent, quarantine_name, label=f"{label} quarantine")
    if not _anchored_snapshot_matches_after_rename(observed, expected):
        raise ReleaseError(f"{label} quarantine changed before restore")
    if _validate_regular_at(parent, public_name, label=label) is not None:
        raise ReleaseError(f"{label} public name was concurrently reoccupied")
    parent.assert_current(phase="immediately before quarantine restore")
    _renameat2(
        parent.descriptor,
        quarantine_name,
        public_name,
        flags=_RENAME_NOREPLACE,
        label=f"{label} quarantine restore",
    )
    restored = _capture_anchored_file(parent, public_name, label=label)
    if not _anchored_snapshot_matches_after_rename(restored, observed):
        raise ReleaseError(f"{label} quarantine changed during restore")
    parent.fsync(label="publication parent")


def _restore_untrusted_quarantined_leaf(
    parent: _DirectoryAnchor,
    quarantine_name: str,
    public_name: str,
    *,
    label: str,
) -> None:
    """Preserve an unsafe displaced leaf without trusting its metadata.

    This is used only after a post-move validation fails (for example because
    the displaced file acquired a hard link).  Moving it into an absent public
    name with ``RENAME_NOREPLACE`` cannot overwrite or unlink a foreign leaf.
    """

    parent.assert_current(phase="before untrusted quarantine restore")
    try:
        _lstat_at(parent.descriptor, public_name, label=label)
    except FileNotFoundError:
        pass
    else:
        raise ReleaseError(f"{label} public name was concurrently reoccupied")
    parent.assert_current(phase="immediately before untrusted quarantine restore")
    _renameat2(
        parent.descriptor,
        quarantine_name,
        public_name,
        flags=_RENAME_NOREPLACE,
        label=f"{label} untrusted quarantine restore",
    )
    parent.fsync(label="publication parent")


def _restore_uncommitted_replacement(record: _PublicationRecord) -> bool:
    """Put a tombstoned preimage back only while its public name is empty.

    A replacement first moves the old public leaf to a private quarantine with
    ``RENAME_NOREPLACE``.  If publishing the stage later fails, restoring that
    preimage is safe only when no other writer has occupied the public name.
    Returning ``False`` deliberately leaves both entries intact in that case:
    overwriting the foreign public entry would recreate the leaf race this
    transaction is intended to close.
    """

    quarantine = record.quarantine
    if quarantine is None:
        return False
    parent = quarantine.parent
    parent.assert_current(phase="before replacement restore")
    try:
        _lstat_at(parent.descriptor, record.name, label=record.label)
    except FileNotFoundError:
        pass
    else:
        return False
    try:
        _restore_quarantined_leaf(
            parent,
            quarantine.temporary_name,
            record.name,
            expected=record.snapshot,
            label=record.label,
        )
    except FileExistsError:
        # A foreign leaf arrived between the absence check and the no-replace
        # restore.  It is still public and the old preimage remains private.
        return False
    quarantine.close()
    record.quarantine = None
    return True


def _require_destination_identity(
    parent: _DirectoryAnchor,
    name: str,
    expected: _FileIdentity | None,
    *,
    label: str,
) -> None:
    observed = _validate_regular_at(parent, name, label=label)
    if observed != expected:
        raise ReleaseError(f"{label} changed before commit")


def _commit_staged_file(
    parents: _PublicationParents,
    record: _PublicationRecord,
    *,
    expected: _FileIdentity | None,
    journal: _PublicationJournal | None = None,
) -> _FileIdentity:
    """Publish a staged leaf through Linux no-replace tombstone operations.

    An absent destination uses ``RENAME_NOREPLACE`` directly.  A replacement
    first moves the verified old target to a private quarantine using the same
    flag, then publishes the stage with another no-replace rename.  The old
    leaf stays quarantined until transaction verification.  Thus an
    intervening foreign destination either wins a no-replace operation or is
    restored into an empty public name; this transaction never overwrites or
    unlinks it.
    """

    staged = record.staged
    if staged is None:  # pragma: no cover - caller constructs it immediately before commit
        raise ReleaseError(f"{record.label} has no staged file")
    parents.assert_current(phase="before commit")
    _require_destination_identity(staged.parent, record.name, expected, label=record.label)
    descriptor_identity = _file_identity(os.fstat(staged.descriptor), label=f"{record.label} temporary")
    named_identity = _validate_regular_at(
        staged.parent,
        staged.temporary_name,
        label=f"{record.label} temporary",
    )
    if (
        descriptor_identity.nlink != 1
        or named_identity is None
        or named_identity.nlink != 1
        or not _same_file_object(descriptor_identity, staged.identity)
        or not _same_file_object(named_identity, staged.identity)
    ):
        raise ReleaseError(f"{record.label} temporary file changed before commit")
    staged_expected = _staged_anchored_snapshot(record, staged.identity)
    named_snapshot = _capture_anchored_file(
        staged.parent,
        staged.temporary_name,
        label=f"{record.label} temporary",
    )
    if not _anchored_snapshot_matches_after_rename(named_snapshot, staged_expected):
        raise ReleaseError(f"{record.label} temporary file changed before commit")

    if expected is None:
        try:
            parents.assert_current(phase="immediately before absent publication")
            _journal_record_phase(journal, record, "publish-intent")
            _renameat2(
                staged.parent.descriptor,
                staged.temporary_name,
                record.name,
                flags=_RENAME_NOREPLACE,
                label=record.label,
            )
        except FileExistsError as exc:
            raise ReleaseError(f"{record.label} changed before commit") from exc
        except BaseException:
            _adopt_interrupted_staged_publication(record, staged, journal=journal)
            raise
        try:
            observed_snapshot = _capture_anchored_file(
                staged.parent,
                record.name,
                label=record.label,
            )
            if not _anchored_snapshot_matches_after_rename(observed_snapshot, staged_expected):
                raise ReleaseError(f"{record.label} destination changed during commit")
            observed = observed_snapshot.identity
            if observed is None:  # pragma: no cover - staged publication is present
                raise ReleaseError(f"{record.label} disappeared during commit")
            record.committed_identity = observed
            staged.parent.fsync(label="publication parent")
            _journal_record_phase(journal, record, "published")
            parents.assert_current(phase="after commit")
            staged.close()
            record.staged = None
            # The durable published frame carries the exact public B
            # identity.  The private stage name was consumed by rename and is
            # no longer a cleanup obligation; clear it in a second durable
            # frame so terminal finalization never mistakes a stale name for
            # a private leaf that still holds A or B.
            record.staged_name = None
            record.staged_identity = None
            _journal_record_phase(journal, record, "published")
            return observed
        except BaseException:
            _adopt_interrupted_staged_publication(record, staged, journal=journal)
            raise

    # A public replacement is deliberately a tombstone sequence instead of a
    # second unconditional exchange.  First move the verified old leaf to a
    # fresh private name without replacing any public name.  Then materialize
    # the stage only into an atomically absent destination.  A writer that
    # wins either boundary remains at the public name; its file is never
    # overwritten, unlinked, or exchanged into a later stale restore.
    _commit_unlink(parents, record, expected=expected, journal=journal)
    try:
        parents.assert_current(phase="before replacement publication")
        _journal_record_phase(journal, record, "publish-intent")
        _renameat2(
            staged.parent.descriptor,
            staged.temporary_name,
            record.name,
            flags=_RENAME_NOREPLACE,
            label=record.label,
        )
    except FileExistsError as exc:
        try:
            _restore_uncommitted_replacement(record)
        except Exception as restore_exc:
            raise ReleaseError(
                f"{record.label} changed before commit and could not be safely restored: {restore_exc}"
            ) from exc
        raise ReleaseError(f"{record.label} changed before commit") from exc
    except Exception as exc:
        try:
            _restore_uncommitted_replacement(record)
        except Exception as restore_exc:
            raise ReleaseError(
                f"{record.label} could not publish its replacement and could not be safely restored: {restore_exc}"
            ) from exc
        raise ReleaseError(f"{record.label} could not publish its replacement") from exc
    except BaseException:
        _adopt_interrupted_staged_publication(record, staged, journal=journal)
        raise

    try:
        try:
            destination = _capture_anchored_file(staged.parent, record.name, label=record.label)
            if not _anchored_snapshot_matches_after_rename(destination, staged_expected):
                raise ReleaseError(f"{record.label} destination changed during commit")
            observed = destination.identity
            if observed is None:  # pragma: no cover - staged publication is present
                raise ReleaseError(f"{record.label} disappeared during commit")
        except Exception as exc:
            try:
                _restore_uncommitted_replacement(record)
            except Exception as restore_exc:
                raise ReleaseError(
                    f"{record.label} changed during commit and could not be safely restored: {restore_exc}"
                ) from exc
            raise ReleaseError(f"{record.label} changed during commit") from exc

        record.committed_identity = observed
        staged.parent.fsync(label="publication parent")
        _journal_record_phase(journal, record, "published")
        parents.assert_current(phase="after commit")
        staged.close()
        record.staged = None
        record.staged_name = None
        record.staged_identity = None
        _journal_record_phase(journal, record, "published")
        return observed
    except BaseException:
        _adopt_interrupted_staged_publication(record, staged, journal=journal)
        raise


def _commit_unlink(
    parents: _PublicationParents,
    record: _PublicationRecord,
    *,
    expected: _FileIdentity,
    expected_snapshot: _AnchoredFileSnapshot | None = None,
    journal: _PublicationJournal | None = None,
) -> None:
    """Remove a public leaf by quarantining it through ``RENAME_NOREPLACE``."""

    parent = parents.for_path(record.path)
    parents.assert_current(phase="before commit")
    _require_destination_identity(parent, record.name, expected, label=record.label)
    anchored_expected = expected_snapshot or record.snapshot
    if anchored_expected.identity is None:  # pragma: no cover - deletion caller guards this
        raise ReleaseError(f"{record.label} has no deletion identity")
    quarantine_name = record.quarantine_name or _new_private_publication_name(
        record.name,
        purpose="quarantine",
    )
    record.quarantine_name = quarantine_name
    _journal_record_phase(journal, record, "quarantine-intent")
    for _attempt in range(32):
        try:
            parents.assert_current(phase="immediately before removal")
            _renameat2(
                parent.descriptor,
                record.name,
                quarantine_name,
                flags=_RENAME_NOREPLACE,
                label=f"{record.label} removal",
            )
            break
        except FileExistsError:
            if journal is not None:
                raise ReleaseError(
                    f"{record.label} journal quarantine name is already occupied"
                ) from None
            quarantine_name = _new_private_publication_name(record.name, purpose="quarantine")
            record.quarantine_name = quarantine_name
        except FileNotFoundError as exc:
            raise ReleaseError(f"{record.label} changed before removal") from exc
        except BaseException:
            # A signal can arrive after the kernel completed renameat2 but
            # before Python observed its return.  Adopt a provably renamed A
            # while the parent FD is still live so BaseException rollback has
            # the same safe tombstone vocabulary as an ordinary failure.  If
            # that inspection is itself interrupted, the prior durable intent
            # frame remains sufficient for restart recovery.
            try:
                interrupted = _capture_anchored_file(
                    parent,
                    quarantine_name,
                    label=f"{record.label} quarantine",
                )
                if _anchored_snapshot_matches_after_rename(interrupted, anchored_expected):
                    adopted = _open_named_staged_file(
                        parent,
                        quarantine_name,
                        label=f"{record.label} quarantine",
                    )
                    record.quarantine = adopted
                    record.quarantine_identity = adopted.identity
                    parent.fsync(label="publication parent")
                    _journal_record_phase(journal, record, "quarantined")
            except BaseException:
                pass
            raise
    else:  # pragma: no cover - UUID collisions are not realistically reachable
        raise ReleaseError(f"could not allocate a quarantine name for {record.label}")

    try:
        quarantined = _capture_anchored_file(
            parent,
            quarantine_name,
            label=f"{record.label} quarantine",
        )
    except Exception as exc:
        try:
            _restore_untrusted_quarantined_leaf(
                parent,
                quarantine_name,
                record.name,
                label=record.label,
            )
        except Exception as restore_exc:
            raise ReleaseError(
                f"{record.label} changed before removal and could not be safely restored: {restore_exc}"
            ) from exc
        raise ReleaseError(f"{record.label} changed before removal") from exc
    if not _anchored_snapshot_matches_after_rename(quarantined, anchored_expected):
        # The public name is empty after the no-replace move.  Restore the
        # displaced object without ever overwriting a concurrently created
        # public leaf; a foreign same-name leaf therefore survives the error.
        try:
            _restore_quarantined_leaf(
                parent,
                quarantine_name,
                record.name,
                expected=quarantined,
                label=record.label,
            )
        except Exception as exc:
            try:
                _restore_untrusted_quarantined_leaf(
                    parent,
                    quarantine_name,
                    record.name,
                    label=record.label,
                )
            except Exception as untrusted_restore_exc:
                raise ReleaseError(
                    f"{record.label} changed before removal and could not be safely restored: {untrusted_restore_exc}"
                ) from exc
            raise ReleaseError(
                f"{record.label} changed before removal and could not be safely restored: {exc}"
            ) from exc
        raise ReleaseError(f"{record.label} changed before removal")
    quarantine: _StagedPublicationFile | None = None
    try:
        quarantine = _open_named_staged_file(
            parent,
            quarantine_name,
            label=f"{record.label} quarantine",
        )
        rechecked = _capture_anchored_file(
            parent,
            quarantine_name,
            label=f"{record.label} quarantine",
        )
        if not _anchored_snapshot_matches_after_rename(rechecked, anchored_expected):
            raise ReleaseError(f"{record.label} quarantine changed during removal")
    except Exception as exc:
        if quarantine is not None:
            quarantine.close()
        # As above, this restore is no-replace and can therefore never erase
        # a foreign leaf that reoccupied the public name.
        try:
            _restore_quarantined_leaf(
                parent,
                quarantine_name,
                record.name,
                expected=quarantined,
                label=record.label,
            )
        except Exception as restore_exc:
            try:
                _restore_untrusted_quarantined_leaf(
                    parent,
                    quarantine_name,
                    record.name,
                    label=record.label,
                )
            except Exception as untrusted_restore_exc:
                raise ReleaseError(
                    f"{record.label} quarantine changed and could not be safely restored: "
                    f"{restore_exc}; {untrusted_restore_exc}"
                ) from exc
        raise ReleaseError(f"{record.label} quarantine changed during removal") from exc
    record.quarantine = quarantine
    record.quarantine_identity = quarantine.identity
    parent.fsync(label="publication parent")
    # This durable record is deliberately written *before* the public-name
    # reoccupation check below.  If an uncoordinated writer has already won,
    # restart recovery still knows that this exact private A belongs to us.
    _journal_record_phase(journal, record, "quarantined")
    if _validate_regular_at(parent, record.name, label=record.label) is not None:
        # A leaf that appeared after the rename belongs to somebody else.  Do
        # not overwrite it with our quarantine; leave both objects intact.
        raise ReleaseError(f"{record.label} was concurrently reoccupied during removal")
    if _validate_regular_at(parent, record.name, label=record.label) is not None:
        raise ReleaseError(f"{record.label} remained after removal")
    parents.assert_current(phase="after commit")


def _finalize_record_quarantine(
    record: _PublicationRecord,
    *,
    journal: _PublicationJournal | None = None,
) -> None:
    """Discard a verified displaced leaf only after transaction verification."""

    quarantine = record.quarantine
    if quarantine is None:
        return
    _dispose_record_private(record, quarantine, source="quarantine", journal=journal)
    quarantine.close()
    record.quarantine = None
    record.quarantine_name = None
    record.quarantine_identity = None
    if journal is not None:
        _journal_record_phase(journal, record, "cleanup-complete")


def _adopt_recorded_rollback_tombstone(
    parent: _DirectoryAnchor,
    record: _PublicationRecord,
    *,
    journal: _PublicationJournal | None,
) -> None:
    """Bind an already-renamed B tombstone to its durable rollback intent.

    A rollback writes ``rollback_name`` and B's pre-rename identity before it
    moves B out of the canonical name.  A signal can land after that
    ``renameat2`` succeeds but before the local helper returns its open
    descriptor.  Reopen only an exact, single-link B at that recorded private
    name so both in-process compensation and restart recovery can resume A's
    no-replace restore.  A different private leaf is never adopted or
    unlinked.
    """

    if record.rollback is not None or record.rollback_name is None:
        return
    expected = _recovery_expected_rollback(record)
    if expected is None:
        return
    observed = _capture_anchored_file(
        parent,
        record.rollback_name,
        label=f"{record.label} rollback tombstone",
    )
    if not observed.snapshot.exists:
        return
    if not _anchored_snapshot_matches_after_rename(observed, expected):
        raise ReleaseError(f"{record.label} rollback tombstone changed before recovery")
    rollback = _open_named_staged_file(
        parent,
        record.rollback_name,
        label=f"{record.label} rollback tombstone",
    )
    if expected.identity is None or not _same_renamed_file_identity(
        rollback.identity,
        expected.identity,
    ):
        rollback.close()
        raise ReleaseError(f"{record.label} rollback tombstone changed while opening")
    record.rollback = rollback
    record.rollback_identity = rollback.identity
    _journal_record_phase(journal, record, "rollback-discarded")


def _restore_record(
    parents: _PublicationParents,
    record: _PublicationRecord,
    *,
    journal: _PublicationJournal | None = None,
) -> None:
    """Undo one completed write/delete only if its postimage is still ours."""

    parent = parents.for_path(record.path)
    parents.assert_current(phase="before rollback")
    _adopt_recorded_rollback_tombstone(parent, record, journal=journal)
    if record.phase in {
        "rollback-discard-intent",
        "rollback-discarded",
        "rollback-restore-intent",
        "rollback-restored",
    } and record.rollback is not None:
        # B was already moved to its registered tombstone.  Resume A's
        # no-replace restore before inspecting or disposing B; a signal here
        # must not make the absent canonical name look like a foreign swap.
        current = _capture_anchored_file(parent, record.name, label=record.label)
        current_is_a = _anchored_snapshot_matches_after_rename(current, record.snapshot)
        # An originally absent first-install target has no A quarantine.
        # Its absence already satisfies rollback after B reached its tombstone.
        if not current.snapshot.exists and record.snapshot.snapshot.exists:
            original_quarantine = record.quarantine
            if original_quarantine is None:
                raise ReleaseError(
                    f"{record.label} lost its original quarantine; rollback is incomplete"
                )
            _journal_record_phase(journal, record, "rollback-restore-intent")
            _restore_quarantined_leaf(
                parent,
                original_quarantine.temporary_name,
                record.name,
                expected=record.snapshot,
                label=f"rollback {record.label}",
            )
            original_quarantine.close()
            record.quarantine = None
            record.quarantine_name = None
            record.quarantine_identity = None
            record.committed_identity = None
            current_is_a = True
            _journal_record_phase(journal, record, "rollback-restored")
        if not current_is_a:
            raise ReleaseError(f"{record.label} has a foreign canonical leaf during rollback")
        rollback = record.rollback
        try:
            _dispose_record_private(record, rollback, source="rollback", journal=journal)
        finally:
            rollback.close()
            record.rollback = None
        record.rollback_name = None
        record.rollback_identity = None
        record.committed_identity = None
        _journal_record_phase(journal, record, "rolled-back")
        return
    if record.committed_identity is None and not record.deleted:
        # A failed replacement can leave A durably quarantined before B ever
        # reached its public name.  It is still a rollback participant: a
        # foreign reoccupation must remain public, while the registered A
        # tombstone remains available for a later retry/restart recovery.
        if record.quarantine is not None:
            _journal_record_phase(journal, record, "rollback-restore-intent")
            if not _restore_uncommitted_replacement(record):
                raise ReleaseError(f"{record.label} public name was concurrently reoccupied")
            record.quarantine_name = None
            record.quarantine_identity = None
            _journal_record_phase(journal, record, "rolled-back")
        if record.staged is not None:
            _dispose_record_private(record, record.staged, source="staged", journal=journal)
            record.staged.close()
            record.staged = None
            record.staged_name = None
            record.staged_identity = None
            _journal_record_phase(journal, record, "rolled-back")
        return
    if record.deleted:
        if _validate_regular_at(parent, record.name, label=record.label) is not None:
            raise ReleaseError(f"{record.label} changed before rollback")
        quarantine = record.quarantine
        if quarantine is not None:
            _journal_record_phase(journal, record, "rollback-restore-intent")
            _restore_quarantined_leaf(
                parent,
                quarantine.temporary_name,
                record.name,
                expected=record.snapshot,
                label=record.label,
            )
            quarantine.close()
            record.quarantine = None
            record.quarantine_name = None
            record.quarantine_identity = None
            record.deleted = False
            _journal_record_phase(journal, record, "rolled-back")
            return
        snapshot = record.snapshot.snapshot
        if not snapshot.exists:
            record.deleted = False
            return
        record.staged = _stage_publication_file(
            parent,
            record.name,
            snapshot.content,
            mode=snapshot.mode,
            label=f"rollback {record.label}",
        )
        record.committed_identity = _commit_staged_file(
            parents,
            record,
            expected=None,
            journal=journal,
        )
        record.deleted = False
        _journal_record_phase(journal, record, "rolled-back")
        return
    if record.committed_identity is None:
        return
    committed = _capture_anchored_file(parent, record.name, label=record.label)
    if not _anchored_snapshot_matches_after_rename(
        committed,
        _staged_anchored_snapshot(record, record.committed_identity),
    ):
        raise ReleaseError(f"{record.label} changed before rollback")
    snapshot = record.snapshot.snapshot
    if record.quarantine is not None:
        # Reverse a replacement with the same no-replace tombstone discipline
        # used to publish it.  An exchange would make a second foreign leaf
        # vulnerable to a stale rollback exchange; moving the current B to a
        # private quarantine first leaves every foreign public name intact.
        original_quarantine = record.quarantine
        displaced = _capture_anchored_file(
            parent,
            original_quarantine.temporary_name,
            label=f"{record.label} quarantine",
        )
        if not _anchored_snapshot_matches_after_rename(displaced, record.snapshot):
            raise ReleaseError(f"{record.label} quarantine changed before rollback")
        committed_snapshot = _staged_anchored_snapshot(
            record,
            record.committed_identity,
        )
        discarded_record = _PublicationRecord(
            record.path,
            record.name,
            None,
            record.mode,
            f"rollback {record.label}",
            committed_snapshot,
        )
        rollback_name = record.rollback_name or _new_private_publication_name(
            record.name,
            purpose="quarantine",
        )
        record.rollback_name = rollback_name
        if (
            record.rollback_identity is not None
            and record.rollback_identity != record.committed_identity
        ):
            raise ReleaseError(f"{record.label} rollback tombstone identity changed")
        # The intent frame must already bind B's exact inode before the
        # no-replace tombstone rename.  If a signal follows that rename before
        # ``discarded_record.quarantine`` is assigned below, recovery can
        # still open and restore this specific B rather than orphaning it.
        record.rollback_identity = record.committed_identity
        discarded_record.quarantine_name = rollback_name
        _journal_record_phase(journal, record, "rollback-discard-intent")
        _commit_unlink(
            parents,
            discarded_record,
            expected=record.committed_identity,
            expected_snapshot=committed_snapshot,
        )
        discarded_quarantine = discarded_record.quarantine
        if discarded_quarantine is None:  # pragma: no cover - commit invariant
            raise ReleaseError(f"{record.label} current leaf was not quarantined for rollback")
        record.rollback_identity = discarded_quarantine.identity
        record.rollback = discarded_quarantine
        _journal_record_phase(journal, record, "rollback-discarded")
        try:
            _journal_record_phase(journal, record, "rollback-restore-intent")
            _restore_quarantined_leaf(
                parent,
                original_quarantine.temporary_name,
                record.name,
                expected=record.snapshot,
                label=f"rollback {record.label}",
            )
        except Exception as exc:
            # If the destination is still empty, put B back without replacing
            # any leaf which might have appeared while A was being restored.
            try:
                _restore_quarantined_leaf(
                    parent,
                    discarded_quarantine.temporary_name,
                    record.name,
                    expected=committed_snapshot,
                    label=f"rollback {record.label} restore current",
                )
            except FileExistsError:
                # A foreign reoccupation is the reason rollback must stop;
                # both quarantines remain available and no public foreign
                # leaf has been overwritten.
                pass
            except Exception:
                # Keep the primary error below.  The private B quarantine is
                # intentionally retained instead of risking an unconditional
                # cleanup or overwrite.
                pass
            discarded_quarantine.close()
            record.rollback = None
            raise ReleaseError(f"{record.label} changed during rollback") from exc

        original_quarantine.close()
        record.quarantine = None
        record.quarantine_name = None
        record.quarantine_identity = None
        record.committed_identity = None
        _journal_record_phase(journal, record, "rollback-restored")
        try:
            _dispose_record_private(record, discarded_quarantine, source="rollback", journal=journal)
        finally:
            discarded_quarantine.close()
            record.rollback = None
        record.rollback_name = None
        record.rollback_identity = None
        _journal_record_phase(journal, record, "rolled-back")
        parent.fsync(label="publication parent")
        parents.assert_current(phase="after rollback")
        return
    if not snapshot.exists:
        committed_snapshot = _staged_anchored_snapshot(record, record.committed_identity)
        discarded_record = _PublicationRecord(
            record.path,
            record.name,
            None,
            record.mode,
            f"rollback {record.label}",
            committed_snapshot,
        )
        record.rollback_name = record.rollback_name or _new_private_publication_name(
            record.name,
            purpose="quarantine",
        )
        if (
            record.rollback_identity is not None
            and record.rollback_identity != record.committed_identity
        ):
            raise ReleaseError(f"{record.label} rollback tombstone identity changed")
        record.rollback_identity = record.committed_identity
        discarded_record.quarantine_name = record.rollback_name
        _journal_record_phase(journal, record, "rollback-discard-intent")
        _commit_unlink(
            parents,
            discarded_record,
            expected=record.committed_identity,
            expected_snapshot=committed_snapshot,
        )
        discarded_quarantine = discarded_record.quarantine
        if discarded_quarantine is None:  # pragma: no cover - commit invariant
            raise ReleaseError(f"{record.label} current leaf was not quarantined for rollback")
        record.rollback_identity = discarded_quarantine.identity
        record.rollback = discarded_quarantine
        _journal_record_phase(journal, record, "rollback-discarded")
        record.committed_identity = None
        try:
            _dispose_record_private(record, discarded_quarantine, source="rollback", journal=journal)
        finally:
            discarded_quarantine.close()
            record.rollback = None
        record.rollback_name = None
        record.rollback_identity = None
        _journal_record_phase(journal, record, "rolled-back")
        return
    record.staged = _stage_publication_file(
        parent,
        record.name,
        snapshot.content,
        mode=snapshot.mode,
        label=f"rollback {record.label}",
    )
    record.committed_identity = _commit_staged_file(
        parents,
        record,
        expected=record.committed_identity,
        journal=journal,
    )
    _finalize_record_quarantine(record, journal=journal)
    _journal_record_phase(journal, record, "rolled-back")


_PUBLICATION_WRITE_PROBE = threading.local()


@contextmanager
def _publication_write_probe() -> Iterator[None]:
    """Retain the historical atomic-write fault-injection seam safely.

    Release tests and integrators have long used ``_atomic_write_bytes`` to
    inject a write failure.  The descriptor transaction now owns the real
    staging/rename work, so calls made while this probe is active deliberately
    do no filesystem work.  A replacement hook can still raise at the same
    per-file boundary without recursing into a second transaction.
    """

    previous = bool(getattr(_PUBLICATION_WRITE_PROBE, "active", False))
    _PUBLICATION_WRITE_PROBE.active = True
    try:
        yield
    finally:
        _PUBLICATION_WRITE_PROBE.active = previous


@contextmanager
def _atomic_write_transaction() -> Iterator[None]:
    """Mark a transaction entered through the public atomic-write helper."""

    previous = bool(getattr(_PUBLICATION_WRITE_PROBE, "atomic_write", False))
    _PUBLICATION_WRITE_PROBE.atomic_write = True
    try:
        yield
    finally:
        _PUBLICATION_WRITE_PROBE.atomic_write = previous


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int = 0o644,
    parent_guards: object | None = None,
) -> None:
    if bool(getattr(_PUBLICATION_WRITE_PROBE, "active", False)):
        return
    with _atomic_write_transaction():
        _publish_files_transactionally(
            ((path, content, mode, f"atomic write {path.name}"),),
            parent_guards=parent_guards,
        )


def _restore_file(
    path: Path,
    snapshot: _FileSnapshot,
    *,
    label: str,
    parent_guards: object | None = None,
) -> None:
    content = snapshot.content if snapshot.exists else None
    _publish_files_transactionally(
        ((path, content, snapshot.mode, label),),
        parent_guards=parent_guards,
    )


def _publish_files_transactionally(
    writes: Sequence[tuple[Path, bytes | None, int, str]],
    *,
    verify: Callable[[], None] | None = None,
    before_publish: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_publish: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
    descriptor_rooted: bool = False,
    parent_guards: object | None = None,
    expected_preimages: Mapping[Path, object] | None = None,
    journal_path: Path | str | os.PathLike[str] | None = None,
    production_probe: bool = False,
) -> None:
    """Publish a small activation set through stable parent directory FDs.

    Every target parent is opened with a no-follow component walk.  The same
    descriptors are used for staging, rename, unlink, directory fsync, and
    rollback.  A fresh no-follow walk must still resolve to the original
    ancestry before any mutation and before every commit or rollback step.

    Canonical destination names are changed only with
    ``renameat2(RENAME_NOREPLACE)``.  Replacement and removal first tombstone
    the verified leaf under a fresh private name, so a foreign leaf that wins
    a stale check is never overwritten or unlinked.  An uncoordinated writer
    can still reoccupy the public name before a safe restore; that fails the
    transaction and leaves the tombstone/quarantine in place rather than
    overwriting the newer leaf.  Linux also has no inode-CAS ``unlinkat``:
    after final cleanup validates a generated private disposal name, a
    same-directory writer that can observe or guess that UUID can replace
    *that private name* before ``unlinkat`` and have its private leaf
    unlinked.  Neither boundary can cause this transaction to overwrite or
    unlink a canonical destination, traverse a replacement ancestor, or touch
    a name outside the anchored directory tree.

    Before private staging or any public rename, this transaction writes a
    create-exclusive, append-only journal and fsyncs each intent/identity
    frame.  A partial append leaves the preceding complete frame parseable.
    ``recover_publication_transaction`` restores only an exact B postimage or
    exact renamed A tombstone; a foreign canonical reoccupation stays public
    and the journal remains retryable.  After verification, a durable
    ``committed`` frame makes cleanup best effort: a post-unlink directory
    fsync failure can leave private residue, but can never make the caller
    claim that A is still available for rollback.  Bounded EINTR retries apply
    to renameat2, unlinkat, and directory/file fsync operations.

    After successful local rollback, ``after_recovery`` receives the exact
    restored identities while the terminal journal remains live.  Failed or
    interrupted handoff retains that journal for restart recovery.
    """

    if descriptor_rooted:
        # The production desktop transaction has its own complete no-follow
        # descriptor walk and parent revalidation.  Keep using that path for
        # its established locking and fault-injection boundary, but bind an
        # authenticated first-install receipt to the exact preimages it saw.
        # Caller-provided anchors cannot be reused there: they belong to this
        # transaction's FD set, so accepting them would weaken the proof.
        if parent_guards is not None:
            raise ReleaseError("descriptor-rooted publication cannot reuse parent guards")
        if journal_path is not None:
            raise ReleaseError("descriptor-rooted publication cannot use a local journal")
        if (
            before_publish is not None
            or after_publish is not None
            or after_recovery is not None
        ):
            raise ReleaseError("descriptor-rooted publication cannot expose a local journal receipt")
        if any(content is None for _path, content, _mode, _label in writes):
            raise ReleaseError("descriptor-rooted publication cannot remove files")
        descriptor_expected: dict[Path, tuple[bool, bytes, int]] = {}
        if expected_preimages is not None:
            for raw_path, value in expected_preimages.items():
                path = _canonical_publication_path(raw_path, label="expected preimage")
                snapshot = _expected_anchored_snapshot_from_object(
                    value,
                    label=f"expected preimage {path}",
                ).snapshot
                descriptor_expected[path] = (
                    snapshot.exists,
                    snapshot.content,
                    snapshot.mode,
                )
        from .desktop_integration import (
            _publish_files_transactionally as publish_descriptor_rooted,
        )

        try:
            publish_descriptor_rooted(
                tuple(writes),
                finalize=verify,
                expected_preimages=descriptor_expected or None,
            )
        except Exception as exc:
            if "rollback was incomplete" in str(exc):
                raise ReleaseError(f"activation publication failed: {exc}") from exc
            raise ReleaseError(
                f"activation publication failed and was rolled back: {exc}"
            ) from exc
        return

    if not writes:
        if (
            before_publish is not None
            or after_publish is not None
            or after_recovery is not None
        ):
            raise ReleaseError("publication callbacks require at least one destination")
        if verify is not None:
            verify()
        return
    normalized: list[tuple[Path, bytes | None, int, str]] = []
    seen: set[Path] = set()
    for raw_path, content, mode, label in writes:
        path = _canonical_publication_path(raw_path, label=label)
        _safe_component(path.name, label=label)
        if path in seen:
            raise ReleaseError(f"publication repeats destination: {path}")
        seen.add(path)
        if content is not None and not isinstance(content, bytes):
            raise ReleaseError(f"publication bytes are invalid for {label}")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
            raise ReleaseError(f"publication mode is invalid for {label}")
        normalized.append((path, content, mode, label))
    expected_map: dict[Path, _AnchoredFileSnapshot] = {}
    if expected_preimages is not None:
        for raw_path, value in expected_preimages.items():
            path = _canonical_publication_path(raw_path, label="expected preimage")
            expected_map[path] = _expected_anchored_snapshot_from_object(
                value,
                label=f"expected preimage {path}",
            )
    parents = _open_publication_parents(
        [path for path, _content, _mode, _label in normalized],
        parent_guards=parent_guards,
        create_parents=True,
    )
    records: list[_PublicationRecord] = []
    journal: _PublicationJournal | None = None
    journal_creation_unsettled = False
    recovery_handoff_pending = False
    try:
        parents.assert_current(phase="before preimage capture")
        for path, content, mode, label in normalized:
            parents.assert_current(phase="during preimage capture")
            snapshot = _capture_anchored_file(
                parents.for_path(path),
                path.name,
                label=label,
            )
            expected = expected_map.get(path)
            if expected is not None:
                if snapshot.snapshot != expected.snapshot or (
                    expected.identity is not None and snapshot.identity != expected.identity
                ) or (
                    expected.sha256 is not None and snapshot.sha256 != expected.sha256
                ):
                    raise ReleaseError(f"{label} changed after preflight")
            records.append(_PublicationRecord(path, path.name, content, mode, label, snapshot))
        if expected_map and set(expected_map) != {record.path for record in records}:
            raise ReleaseError("expected publication preimages do not match destinations")

        # The append-only journal exists and is fsynced before the first
        # private stage, quarantine, or public rename.  Every later mutation
        # has a durable intent frame, so a process death or KeyboardInterrupt
        # can be resumed through _recover_publication_transaction().
        try:
            journal = _PublicationJournal.create(
                parents,
                records,
                journal_path=journal_path,
            )
        except _PublicationJournalCreateError as exc:
            journal = exc.journal
            journal_creation_unsettled = True
            # Nothing has been staged or renamed.  The exact journal leaf is
            # intentionally retained for inspection/recovery instead of
            # pretending a normal rollback could remove an incompletely
            # durable create-exclusive record.
            if not isinstance(exc.original_cause, Exception):
                # The journal is now retained, but cancellation must preserve
                # its BaseException semantics.  Do not turn KeyboardInterrupt
                # or SystemExit into an ordinary publication failure.
                raise exc.original_cause
            raise ReleaseError(
                "publication journal initialization was not durably resolved; "
                f"recovery handle retained: {exc.journal.path}"
            ) from exc
        except _PublicationJournalPreflightError:
            # No staging, tombstone, or canonical rename has started.  This
            # is intentionally not routed through generic rollback wording:
            # the setup failure may have preserved a foreign journal-name
            # winner that we must not describe as our rolled-back work.
            raise
        if before_publish is not None:
            before_publish(
                _publication_transaction_receipt(
                    journal,
                    parents,
                    records,
                    state="intent",
                )
            )

        # Do not call the compatibility hook again when this transaction was
        # itself entered through _atomic_write_bytes.  Besides avoiding a
        # recursive transaction, that preserves the one-call ordering contract
        # for users which instrument that public helper.
        use_write_probe = not bool(
            getattr(_PUBLICATION_WRITE_PROBE, "atomic_write", False)
        )
        probe = _publication_write_probe() if use_write_probe else nullcontext()
        with probe:
            for record in records:
                if record.content is None:
                    continue
                # Preserve the established failure-injection boundary while
                # the actual write remains descriptor-relative below.
                if use_write_probe:
                    if production_probe:
                        # Production installs retain the release manager's
                        # journal and descriptor transaction, but also cross
                        # the desktop integration write boundary.  The
                        # desktop helper's probe is intentionally inert; its
                        # parent-swap fault seam still runs before the local
                        # anchored identity check below.
                        from .desktop_integration import (
                            _atomic_write_bytes as desktop_atomic_write,
                        )

                        desktop_atomic_write(
                            record.path,
                            record.content,
                            mode=record.mode,
                            probe=True,
                        )
                    else:
                        _atomic_write_bytes(record.path, record.content, mode=record.mode)
                parents.assert_current(phase="before mutation")
                _require_destination_identity(
                    parents.for_path(record.path),
                    record.name,
                    record.snapshot.identity,
                    label=record.label,
                )
                record.staged_name = _new_private_publication_name(
                    record.name,
                    purpose="tmp",
                )
                _journal_record_phase(journal, record, "stage-intent")
                staged = _stage_publication_file(
                    parents.for_path(record.path),
                    record.name,
                    record.content,
                    mode=record.mode,
                    label=record.label,
                    temporary_name=record.staged_name,
                    record=record,
                    journal=journal,
                )
                # Third-party fault-injection hooks may retain the historical
                # helper signature and return a staged file without the
                # optional journal binding.  Preserve that seam while the
                # native helper records the identity before it returns.
                if record.staged is not staged:
                    record.staged = staged
                    record.staged_identity = staged.identity
                    _journal_record_phase(journal, record, "staged")

        parents.assert_current(phase="before commit")
        # Preserve the caller's ordered transaction boundary.  State writers
        # deliberately append their selecting state last; grouping all writes
        # before all removals would otherwise publish state before an earlier
        # legacy-wrapper deletion.  Every individual operation remains
        # descriptor-journaled, so interleaving write and unlink records does
        # not weaken the rollback proof.
        for record in records:
            if record.content is not None:
                record.committed_identity = _commit_staged_file(
                    parents,
                    record,
                    expected=record.snapshot.identity,
                    journal=journal,
                )
                continue
            if not record.snapshot.snapshot.exists:
                continue
            identity = record.snapshot.identity
            if identity is None:  # pragma: no cover - snapshot invariants above
                raise ReleaseError(f"{record.label} has no deletion identity")
            _commit_unlink(parents, record, expected=identity, journal=journal)
            record.deleted = True
            _journal_record_phase(journal, record, "deleted")

        parents.assert_current(phase="before verification")
        if verify is not None:
            verify()
        parents.assert_current(phase="after verification")
        if after_publish is not None:
            after_publish(
                _publication_transaction_receipt(
                    journal,
                    parents,
                    records,
                    state="published",
                    postimages=_capture_published_postimages(parents, records),
                )
            )
        parents.assert_current(phase="after publication receipt")
        # From this durable point onward B is the committed outcome.  Cleanup
        # can be retried after a crash but must never make this path claim that
        # its discarded A preimage is still required for rollback.
        journal.persist(state="committed")
        for record in records:
            try:
                _finalize_record_quarantine(record, journal=journal)
            except Exception:
                # Keep the committed journal and private preimage for restart
                # cleanup.  A cleanup fsync error is not a failed publication.
                pass
        parents.assert_current(phase="after quarantine cleanup")
    except BaseException as exc:
        if isinstance(exc, _PublicationJournalPreflightError):
            raise
        if journal_creation_unsettled:
            # The first journal frame may exist but its directory entry was
            # not proven durable.  No transaction mutation has occurred, and
            # this process must preserve the opaque handle rather than append
            # a misleading rollback terminal frame or delete it.
            if not isinstance(exc, Exception):
                raise
            raise ReleaseError(str(exc)) from exc
        if journal is not None and journal.durability_uncertain:
            # An append/fsync error can leave either the old frame or the
            # attempted frame durable.  Do not publish a made-up local
            # outcome and do not make another canonical mutation; the exact
            # retained journal is the recovery handle for a fresh process.
            if not isinstance(exc, Exception):
                raise
            raise ReleaseError(
                "activation publication journal durability is uncertain; "
                f"restart recovery is required: {journal.path}"
            ) from exc
        if journal is not None and journal.state == "committed":
            # Never roll back a transaction after its committed frame.  An
            # ordinary cleanup failure is reported accurately; cancellation
            # keeps its original BaseException while recovery cleans residue.
            if isinstance(exc, Exception):
                raise ReleaseError(
                    f"activation publication committed; cleanup deferred: {exc}"
                ) from exc
            raise
        # Set this before any rollback terminal frame can become durable.
        # A signal between that frame and callback entry must retain the
        # journal just as a failed callback does.
        recovery_handoff_pending = after_recovery is not None
        rollback_errors: list[str] = []
        rollback_interruption: BaseException | None = None
        if journal is not None:
            try:
                journal.persist(state="rolling-back")
            except BaseException as journal_exc:
                rollback_errors.append(f"journal: {journal_exc}")
                if not isinstance(journal_exc, Exception):
                    rollback_interruption = journal_exc
        try:
            parents.assert_current(phase="before rollback")
        except BaseException as rollback_exc:
            rollback_errors.append(f"parent identity: {rollback_exc}")
            if not isinstance(rollback_exc, Exception):
                rollback_interruption = rollback_exc
        else:
            for record in records:
                try:
                    unknown_private = _unregistered_stage_intent_present(parents, record)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{record.label}: {rollback_exc}")
                    if not isinstance(rollback_exc, Exception):
                        rollback_interruption = rollback_exc
                    continue
                if unknown_private is not None:
                    rollback_errors.append(f"{record.label}: {unknown_private}")
                    continue
                # A successful no-replace rename can be followed by a
                # hard-link or foreign-leaf swap before the first capture
                # records B's canonical identity.  In that interval the
                # staged temporary name is already absent, so blindly
                # cleaning it would otherwise let the transaction claim a
                # completed rollback while unowned B remains public.
                ambiguous = _untrusted_public_postimage_present(parents, record)
                if ambiguous is not None:
                    rollback_errors.append(f"{record.label}: {ambiguous}")
            if not rollback_errors:
                for record in reversed(records):
                    if (
                        record.committed_identity is None
                        and not record.deleted
                        and record.staged is None
                        and record.quarantine is None
                    ):
                        continue
                    try:
                        _restore_record(parents, record, journal=journal)
                    except BaseException as rollback_exc:
                        rollback_errors.append(f"{record.label}: {rollback_exc}")
                        if not isinstance(rollback_exc, Exception):
                            rollback_interruption = rollback_exc
        if not rollback_errors and journal is not None:
            try:
                journal.persist(state="rolled-back")
            except BaseException as journal_exc:
                rollback_errors.append(f"journal finalization: {journal_exc}")
                if not isinstance(journal_exc, Exception):
                    rollback_interruption = journal_exc
        if not rollback_errors and journal is not None and after_recovery is not None:
            try:
                receipt = _publication_transaction_receipt(
                    journal,
                    parents,
                    records,
                    state="recovered",
                    postimages=_capture_recovered_postimages(parents, records),
                )
                after_recovery(receipt)
                parents.assert_current(phase="after rollback recovery receipt")
            except BaseException as recovery_exc:
                rollback_errors.append(f"recovery handoff pending: {recovery_exc}")
                if not isinstance(recovery_exc, Exception):
                    rollback_interruption = recovery_exc
            else:
                recovery_handoff_pending = False
        if rollback_errors:
            if rollback_interruption is not None:
                raise rollback_interruption
            if not isinstance(exc, Exception):
                raise
            raise ReleaseError(
                f"activation publication failed: {exc}; rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if not isinstance(exc, Exception):
            raise
        raise ReleaseError(f"activation publication failed and was rolled back: {exc}") from exc
    finally:
        # Terminal records may be removed only after their fsynced terminal
        # frame.  Failed private cleanup deliberately leaves a durable journal
        # rather than converting a committed B into a misleading rollback.
        if journal is not None:
            if journal_creation_unsettled or recovery_handoff_pending:
                journal.close()
            elif (
                not journal.durability_uncertain
                and journal.state in {"committed", "rolled-back"}
            ):
                journal.finalize_terminal()
            else:
                journal.close()
        for record in records:
            if record.staged is not None:
                record.staged.close()
            if record.quarantine is not None:
                record.quarantine.close()
            if record.rollback is not None:
                record.rollback.close()
            if record.disposal is not None:
                record.disposal.close()
        parents.close()


def _journal_optional_private_name(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseError(f"{label} is malformed")
    return _safe_component(value, label=label)


def _journal_private_name_matches(
    private_name: str,
    public_name: str,
    *,
    purpose: str,
) -> bool:
    """Accept only names this transaction's private-name generator can own."""

    prefix = f".{public_name}.{purpose}-"
    return private_name.startswith(prefix) and len(private_name) > len(prefix)


def _journal_record_from_payload(
    value: object,
    *,
    guards: _PublicationParentGuards,
) -> _PublicationRecord:
    expected = {
        "path", "name", "content_b64", "mode", "label", "snapshot",
        "staged_name", "staged_identity", "quarantine_name", "quarantine_identity",
        "rollback_name", "rollback_identity", "disposal_name", "disposal_identity",
        "disposal_source", "committed_identity", "deleted", "phase",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ReleaseError("publication journal record is malformed")
    raw_path = value["path"]
    name = value["name"]
    content_b64 = value["content_b64"]
    mode = value["mode"]
    label = value["label"]
    if not isinstance(raw_path, str) or not isinstance(name, str) or not isinstance(label, str):
        raise ReleaseError("publication journal record is malformed")
    path = _canonical_publication_path(raw_path, label="publication journal destination")
    if name != path.name or guards.for_path(path) is None:
        raise ReleaseError("publication journal destination is malformed")
    _safe_component(name, label="publication journal destination")
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
        raise ReleaseError("publication journal record is malformed")
    if content_b64 is None:
        content = None
    elif isinstance(content_b64, str):
        try:
            content = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ReleaseError("publication journal record is malformed") from exc
    else:
        raise ReleaseError("publication journal record is malformed")
    snapshot = _journal_snapshot_from_payload(value["snapshot"], label="publication journal")
    phase = value["phase"]
    if phase not in {
        "prepared", "stage-intent", "staged", "quarantine-intent", "quarantined",
        "publish-intent", "published", "deleted", "rollback-discard-intent",
        "rollback-discarded", "rollback-restore-intent", "rollback-restored",
        "cleanup-intent", "cleanup-disposed", "cleanup-complete", "rolled-back",
    }:
        raise ReleaseError("publication journal phase is malformed")
    deleted = value["deleted"]
    if not isinstance(deleted, bool):
        raise ReleaseError("publication journal record is malformed")
    result = _PublicationRecord(path, name, content, mode, label, snapshot)
    result.staged_name = _journal_optional_private_name(value["staged_name"], label="journal stage name")
    result.staged_identity = _journal_file_identity_from_payload(
        value["staged_identity"], label="journal stage"
    )
    result.quarantine_name = _journal_optional_private_name(
        value["quarantine_name"], label="journal quarantine name"
    )
    result.quarantine_identity = _journal_file_identity_from_payload(
        value["quarantine_identity"], label="journal quarantine"
    )
    result.rollback_name = _journal_optional_private_name(
        value["rollback_name"], label="journal rollback name"
    )
    result.rollback_identity = _journal_file_identity_from_payload(
        value["rollback_identity"], label="journal rollback"
    )
    result.disposal_name = _journal_optional_private_name(
        value["disposal_name"], label="journal disposal name"
    )
    result.disposal_identity = _journal_file_identity_from_payload(
        value["disposal_identity"], label="journal disposal"
    )
    result.disposal_source = value["disposal_source"]
    result.committed_identity = _journal_file_identity_from_payload(
        value["committed_identity"], label="journal postimage"
    )
    if result.staged_identity is not None and result.staged_name is None:
        raise ReleaseError("publication journal stage is malformed")
    if result.quarantine_identity is not None and result.quarantine_name is None:
        raise ReleaseError("publication journal quarantine is malformed")
    if result.rollback_identity is not None and result.rollback_name is None:
        raise ReleaseError("publication journal rollback is malformed")
    if (result.disposal_name is None) != (result.disposal_identity is None):
        raise ReleaseError("publication journal disposal is malformed")
    if result.disposal_name is None:
        if result.disposal_source is not None:
            raise ReleaseError("publication journal disposal is malformed")
    elif result.disposal_source not in {"staged", "quarantine", "rollback"}:
        raise ReleaseError("publication journal disposal is malformed")
    if result.staged_name is not None and (
        content is None
        or not _journal_private_name_matches(result.staged_name, name, purpose="tmp")
    ):
        raise ReleaseError("publication journal stage name is malformed")
    if result.quarantine_name is not None and (
        not snapshot.snapshot.exists
        or not _journal_private_name_matches(result.quarantine_name, name, purpose="quarantine")
    ):
        raise ReleaseError("publication journal quarantine name is malformed")
    if result.rollback_name is not None and (
        content is None
        or not _journal_private_name_matches(result.rollback_name, name, purpose="quarantine")
    ):
        raise ReleaseError("publication journal rollback name is malformed")
    if result.disposal_name is not None:
        source_name = {
            "staged": result.staged_name,
            "quarantine": result.quarantine_name,
            "rollback": result.rollback_name,
        }[str(result.disposal_source)]
        if source_name is None or not _journal_private_name_matches(
            result.disposal_name,
            source_name,
            purpose="dispose",
        ):
            raise ReleaseError("publication journal disposal name is malformed")
    result.deleted = deleted
    result.phase = phase
    return result


def _normalize_publication_journal_mode(path: Path) -> None:
    """Restore a private journal to its authenticated reader mode.

    The create-exclusive leaf can briefly inherit a restrictive umask before
    its explicit fchmod.  Recovery must be able to consume that same inode,
    while refusing any group/other-readable object under the journal name.
    """

    parent = _open_directory_anchor(
        path.parent,
        label="publication journal parent",
        create_parents=False,
    )
    try:
        parent.assert_current(phase="before journal mode normalization")
        identity = _validate_regular_at(parent, path.name, label="publication journal")
        if identity is None:
            return
        if identity.mode & ~0o600:
            raise ReleaseError("publication journal has unsafe permissions")
        if identity.mode != 0o600:
            try:
                os.chmod(
                    path.name,
                    0o600,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
            except (OSError, TypeError) as exc:
                raise ReleaseError("publication journal mode could not be normalized") from exc
            parent.fsync(label="publication journal mode")
            updated = _validate_regular_at(parent, path.name, label="publication journal")
            if updated is None or updated.device != identity.device or updated.inode != identity.inode:
                raise ReleaseError("publication journal changed during mode normalization")
    finally:
        parent.close()


def _consume_empty_publication_journal(
    path: Path,
    capture: _AnchoredFileSnapshot,
) -> None:
    """Consume an O_EXCL journal whose first frame never became durable."""

    if capture.identity is None or capture.snapshot.content != b"":
        raise ReleaseError("empty publication journal capture is invalid")
    parent = _open_directory_anchor(
        path.parent,
        label="publication journal parent",
        create_parents=False,
    )
    descriptor = -1
    try:
        parent.assert_current(phase="before empty journal cleanup")
        descriptor = _openat(
            parent.descriptor,
            path.name,
            _file_open_flags(),
            label="publication journal",
        )
        opened = _file_identity(os.fstat(descriptor), label="publication journal")
        named = _validate_regular_at(parent, path.name, label="publication journal")
        if (
            named is None
            or not _same_file_object(opened, capture.identity)
            or not _same_file_object(named, capture.identity)
        ):
            raise ReleaseError("empty publication journal changed before cleanup")
        _unlinkat(parent.descriptor, path.name, label="empty publication journal")
        parent.fsync(label="publication journal parent")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        parent.close()


def _read_publication_journal(
    journal_path: Path | str | os.PathLike[str],
    *,
    recover_disposal: bool = True,
) -> tuple[Path, _AnchoredFileSnapshot, dict[str, object]] | None:
    path = _canonical_publication_path(journal_path, label="publication journal")
    _publication_journal_name(path.name)
    _normalize_publication_journal_mode(path)
    captures = _capture_anchored_files_safely(
        ((path, "publication journal"),),
        create_parents=False,
    )
    capture = captures[path]
    if (
        not capture.snapshot.exists
        and recover_disposal
        and _restore_disposed_publication_journal(path)
    ):
        return _read_publication_journal(path, recover_disposal=False)
    if capture.snapshot.exists and capture.snapshot.content == b"":
        _consume_empty_publication_journal(path, capture)
        return None
    if (
        not capture.snapshot.exists
        or capture.identity is None
        or capture.snapshot.mode != 0o600
        or len(capture.snapshot.content) > _MAX_PUBLICATION_JOURNAL_BYTES
    ):
        raise ReleaseError("publication journal is unavailable or unsafe")
    payload = _publication_journal_latest_payload(capture.snapshot.content)
    expected = {"schema", "generation", "state", "journal_parent", "parents", "records"}
    if set(payload) != expected or payload.get("schema") != _PUBLICATION_JOURNAL_SCHEMA:
        raise ReleaseError("publication journal is malformed")
    generation = payload["generation"]
    state = payload["state"]
    journal_parent = payload["journal_parent"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or state not in {"active", "rolling-back", "committed", "rolled-back"}
        or not isinstance(journal_parent, str)
        or _canonical_publication_path(journal_parent, label="publication journal parent") != path.parent
        or not isinstance(payload["records"], list)
        or not payload["records"]
    ):
        raise ReleaseError("publication journal is malformed")
    return path, capture, payload


def _recovery_expected_stage(record: _PublicationRecord) -> _AnchoredFileSnapshot | None:
    if record.content is None:
        return None
    identity = record.staged_identity
    if identity is None:
        return None
    return _AnchoredFileSnapshot(
        _FileSnapshot(True, record.content, record.mode),
        identity,
        sha256_bytes(record.content),
    )


def _recovery_expected_quarantine(record: _PublicationRecord) -> _AnchoredFileSnapshot | None:
    if not record.snapshot.snapshot.exists:
        return None
    identity = record.quarantine_identity or record.snapshot.identity
    if identity is None:
        return None
    return _AnchoredFileSnapshot(record.snapshot.snapshot, identity, record.snapshot.sha256)


def _recovery_expected_rollback(record: _PublicationRecord) -> _AnchoredFileSnapshot | None:
    """Return the exact B tombstone expected during an interrupted rollback."""

    if record.rollback_name is None or record.content is None:
        return None
    identity = (
        record.rollback_identity
        or record.committed_identity
        or record.staged_identity
    )
    if identity is None:
        return None
    return _AnchoredFileSnapshot(
        _FileSnapshot(True, record.content, record.mode),
        identity,
        sha256_bytes(record.content),
    )


def _recovery_expected_disposal(record: _PublicationRecord) -> _AnchoredFileSnapshot | None:
    """Return the exact private object moved to a registered disposal name."""

    identity = record.disposal_identity
    source = record.disposal_source
    if identity is None or source is None:
        return None
    if source == "quarantine":
        if not record.snapshot.snapshot.exists:
            return None
        return _AnchoredFileSnapshot(
            record.snapshot.snapshot,
            identity,
            record.snapshot.sha256,
        )
    if record.content is None:
        return None
    return _AnchoredFileSnapshot(
        _FileSnapshot(True, record.content, record.mode),
        identity,
        sha256_bytes(record.content),
    )


def _open_recovery_journal(
    parents: _PublicationParents,
    path: Path,
    capture: _AnchoredFileSnapshot,
    payload: Mapping[str, object],
    records: list[_PublicationRecord],
) -> _PublicationJournal:
    """Reopen the exact append-only journal through its guarded parent FD."""

    parent = parents.for_path(path)
    descriptor = _openat(
        parent.descriptor,
        path.name,
        os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | _nofollow_open_flag(),
        label="publication journal",
    )
    try:
        opened = _file_identity(os.fstat(descriptor), label="publication journal")
        named = _validate_regular_at(parent, path.name, label="publication journal")
        if (
            capture.identity is None
            or opened.nlink != 1
            or named is None
            or not _same_file_object(opened, capture.identity)
            or not _same_file_object(named, capture.identity)
        ):
            raise ReleaseError("publication journal changed before restart recovery")
        generation = payload.get("generation")
        state = payload.get("state")
        if isinstance(generation, bool) or not isinstance(generation, int) or not isinstance(state, str):
            raise ReleaseError("publication journal is malformed")
        return _PublicationJournal(
            parent,
            path.name,
            descriptor,
            records,
            parents.anchors(),
            generation=generation,
            state=state,
        )
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _open_recovery_private_file(
    parent: _DirectoryAnchor,
    name: str | None,
    expected: _AnchoredFileSnapshot | None,
    *,
    label: str,
) -> _StagedPublicationFile | None:
    if name is None:
        return None
    if expected is None:
        # An intent frame without a durable file identity never authorizes
        # removing or restoring a private name that another writer may have
        # occupied after the interruption.  If it exists, retain the journal
        # as an explicit recovery obligation instead of deleting the journal
        # and orphaning an unregistered staged leaf.
        observed = _capture_anchored_file(parent, name, label=label)
        if observed.snapshot.exists:
            raise ReleaseError(
                f"{label} is present without a durable identity; "
                "restart recovery is incomplete"
            )
        return None
    observed = _capture_anchored_file(parent, name, label=label)
    if not observed.snapshot.exists:
        return None
    if not _anchored_snapshot_matches_after_rename(observed, expected):
        raise ReleaseError(f"{label} changed before restart recovery")
    opened = _open_named_staged_file(parent, name, label=label)
    if not _same_renamed_file_identity(opened.identity, observed.identity):
        opened.close()
        raise ReleaseError(f"{label} changed while opening for restart recovery")
    return opened


def _unregistered_stage_intent_present(
    parents: _PublicationParents,
    record: _PublicationRecord,
) -> str | None:
    """Report a staged leaf created after its intent frame but before identity.

    The native staging helper records identity before returning, but an abrupt
    process death can still occur after the file reaches storage and before
    the identity frame is fsynced.  The name is durable, but without the full
    identity it is never safe to unlink.  Retaining the journal is preferable
    to declaring the rollback complete and losing the only recovery handle.
    """

    if record.staged_name is None or record.staged_identity is not None:
        return None
    observed = _capture_anchored_file(
        parents.for_path(record.path),
        record.staged_name,
        label=f"{record.label} staged temporary",
    )
    if not observed.snapshot.exists:
        return None
    return "staged temporary is present without a durable identity"


def _recovery_committed_snapshot(
    record: _PublicationRecord,
    observed: _AnchoredFileSnapshot,
) -> _FileIdentity | None:
    if record.content is None or not observed.snapshot.exists or observed.identity is None:
        return None
    # A publish-intent frame already carries the exact staged inode.  Do not
    # downcast an interrupted B publication to matching bytes/mode: a foreign
    # same-content replacement must not gain rollback authority.
    expected_identity = record.committed_identity or record.staged_identity
    if expected_identity is None:
        # A journal intent without an exact B identity cannot authorize a
        # content/mode match at the public name.  That name may have been
        # occupied by an uncoordinated writer after the interruption.
        return None
    expected = _AnchoredFileSnapshot(
        _FileSnapshot(True, record.content, record.mode),
        expected_identity,
        sha256_bytes(record.content),
    )
    if not _anchored_snapshot_matches_after_rename(observed, expected):
        return None
    return observed.identity


def _cleanup_recovery_private(staged: _StagedPublicationFile | None) -> None:
    if staged is None:
        return
    try:
        _cleanup_staged_file(staged)
    finally:
        staged.close()


def _unlink_registered_disposal(staged: _StagedPublicationFile) -> None:
    """Finish a previously journaled disposal without inventing another name."""

    staged.parent.assert_current(phase="before registered disposal cleanup")
    current = _validate_regular_at(
        staged.parent,
        staged.temporary_name,
        label=f"{staged.label} registered disposal",
    )
    if current is None:
        return
    # Recovery opened this already-renamed disposal and captured its current
    # identity.  No rename since that capture can justify ignoring ctime.
    if current.nlink != 1 or current != staged.identity:
        raise ReleaseError(f"{staged.label} registered disposal changed before cleanup")
    staged.parent.assert_current(phase="immediately before registered disposal cleanup")
    _unlinkat(staged.parent.descriptor, staged.temporary_name, label=staged.label)
    staged.parent.fsync(label="publication parent")


def _finish_recovery_disposal(
    record: _PublicationRecord,
    journal: _PublicationJournal,
) -> None:
    """Clear a durable disposal obligation only after its anchored cleanup."""

    disposal = record.disposal
    if disposal is not None:
        try:
            _unlink_registered_disposal(disposal)
        finally:
            disposal.close()
            record.disposal = None
    # A missing registered disposal is also terminally safe: it is a private
    # leaf and the journal already declares either committed B or rollback A.
    # Persisting the cleared obligation is what permits journal finalization.
    record.disposal_name = None
    record.disposal_identity = None
    record.disposal_source = None
    _journal_record_phase(journal, record, "cleanup-complete")


def _untrusted_public_postimage_present(
    parents: _PublicationParents,
    record: _PublicationRecord,
) -> str | None:
    """Refuse a post-rename leaf which cannot be tied to a durable B inode."""

    if record.content is None or record.committed_identity is not None:
        return None
    # Only a record that reached staging/publish intent could have placed B.
    # Before that point an ordinary preimage A remains a valid canonical leaf.
    if record.staged_identity is None and record.phase not in {"publish-intent", "published"}:
        return None
    parent = parents.for_path(record.path)
    try:
        observed = _validate_regular_at(parent, record.name, label=record.label)
    except ReleaseError as exc:
        return f"public postimage could not be authenticated: {exc}"
    if observed is None:
        return None
    if record.snapshot.identity is not None and observed == record.snapshot.identity:
        return None
    return "foreign canonical leaf could not be authenticated"


def _recover_publication_transaction(
    journal_path: Path | str | os.PathLike[str],
    *,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    """Resume one interrupted descriptor-rooted publication journal.

    Recovery itself is journaled.  In particular, every rollback tombstone,
    A restore, and private disposal is preceded by a durable record, so an
    interrupt during recovery simply resumes from the last exact identity
    rather than treating an already-moved B or A as an unowned leaf.
    """

    journal_read = _read_publication_journal(journal_path)
    if journal_read is None:
        return {
            "ok": True,
            "journal": str(_canonical_publication_path(journal_path, label="publication journal")),
            "state": "empty-cleaned",
        }
    path, journal_capture, payload = journal_read
    guards = _journal_directory_guards_from_payload(payload["parents"])
    records = [
        _journal_record_from_payload(item, guards=guards)
        for item in payload["records"]
    ]
    if len({record.path for record in records}) != len(records):
        raise ReleaseError("publication journal repeats a destination")
    parents = _open_publication_parents(
        [record.path for record in records],
        parent_guards=guards,
        create_parents=False,
    )
    journal: _PublicationJournal | None = None
    private_handles: list[_StagedPublicationFile] = []
    try:
        parents.assert_current(phase="before restart recovery")
        journal_parent = parents.for_path(path)
        fresh_journal = _capture_anchored_file(
            journal_parent,
            path.name,
            label="publication journal",
        )
        if (
            fresh_journal.identity != journal_capture.identity
            or fresh_journal.sha256 != journal_capture.sha256
        ):
            raise ReleaseError("publication journal changed before restart recovery")
        journal = _open_recovery_journal(
            parents,
            path,
            journal_capture,
            payload,
            records,
        )
        original_state = str(payload["state"])

        def open_private(record: _PublicationRecord) -> None:
            parent = parents.for_path(record.path)
            record.staged = _open_recovery_private_file(
                parent,
                record.staged_name,
                _recovery_expected_stage(record),
                label=f"{record.label} staged temporary",
            )
            if record.staged is not None:
                private_handles.append(record.staged)
            record.quarantine = _open_recovery_private_file(
                parent,
                record.quarantine_name,
                _recovery_expected_quarantine(record),
                label=f"{record.label} quarantine",
            )
            if record.quarantine is not None:
                private_handles.append(record.quarantine)
            record.rollback = _open_recovery_private_file(
                parent,
                record.rollback_name,
                _recovery_expected_rollback(record),
                label=f"{record.label} rollback tombstone",
            )
            if record.rollback is not None:
                private_handles.append(record.rollback)
            record.disposal = _open_recovery_private_file(
                parent,
                record.disposal_name,
                _recovery_expected_disposal(record),
                label=f"{record.label} registered disposal",
            )
            if record.disposal is not None:
                private_handles.append(record.disposal)

        if original_state in {"committed", "rolled-back"}:
            # A terminal journal never inspects a canonical destination as a
            # rollback candidate.  It owns only exact private names recorded
            # before their moves, and records each cleanup result before the
            # terminal journal can disappear.
            for record in records:
                open_private(record)
                if record.disposal is not None or record.disposal_name is not None:
                    disposal = record.disposal
                    _finish_recovery_disposal(record, journal)
                    if disposal is not None and disposal in private_handles:
                        private_handles.remove(disposal)
                if record.staged is not None or record.staged_name is not None:
                    staged = record.staged
                    if staged is not None:
                        _dispose_record_private(record, staged, source="staged", journal=journal)
                        staged.close()
                        if staged in private_handles:
                            private_handles.remove(staged)
                    record.staged = None
                    record.staged_name = None
                    record.staged_identity = None
                    _journal_record_phase(journal, record, "cleanup-complete")
                if record.quarantine is not None or record.quarantine_name is not None:
                    quarantine = record.quarantine
                    if quarantine is not None:
                        _dispose_record_private(record, quarantine, source="quarantine", journal=journal)
                        quarantine.close()
                        if quarantine in private_handles:
                            private_handles.remove(quarantine)
                    record.quarantine = None
                    record.quarantine_name = None
                    record.quarantine_identity = None
                    _journal_record_phase(journal, record, "cleanup-complete")
                if record.rollback is not None or record.rollback_name is not None:
                    rollback = record.rollback
                    if rollback is not None:
                        _dispose_record_private(record, rollback, source="rollback", journal=journal)
                        rollback.close()
                        if rollback in private_handles:
                            private_handles.remove(rollback)
                    record.rollback = None
                    record.rollback_name = None
                    record.rollback_identity = None
                    _journal_record_phase(journal, record, "cleanup-complete")
            outcome = "committed-cleaned" if original_state == "committed" else "rolled-back"
        else:
            # Durable before the first recovery mutation.  A SIGINT after B's
            # tombstone or A's no-replace restore therefore resumes with the
            # latest recorded exact identities rather than an old active frame.
            journal.persist(state="rolling-back")
            for record in reversed(records):
                open_private(record)
                unknown_private = _unregistered_stage_intent_present(parents, record)
                if unknown_private is not None:
                    raise ReleaseError(
                        f"{record.label}: {unknown_private}; restart recovery is incomplete"
                    )
                ambiguous = _untrusted_public_postimage_present(parents, record)
                if ambiguous is not None:
                    raise ReleaseError(
                        f"{record.label}: {ambiguous}; restart recovery is incomplete"
                    )
                parent = parents.for_path(record.path)
                current = _capture_anchored_file(parent, record.name, label=record.label)
                committed = _recovery_committed_snapshot(record, current)
                if committed is not None:
                    record.committed_identity = committed
                elif current.snapshot.exists and not _anchored_snapshot_matches_after_rename(
                    current, record.snapshot
                ):
                    raise ReleaseError(
                        f"{record.label} has a foreign canonical leaf; restart recovery is incomplete"
                    )
                if (
                    record.content is None
                    and record.quarantine is not None
                    and not current.snapshot.exists
                ):
                    record.deleted = True

                # _restore_record journals rollback-discard, A-restore, and
                # B-disposal transitions.  It is intentionally used for both
                # ordinary active and previously rolling-back records; every
                # operation is idempotent from the newest frame.
                if (
                    record.committed_identity is not None
                    or record.deleted
                    or record.staged is not None
                    or record.quarantine is not None
                    or record.rollback is not None
                    or record.phase in {
                        "rollback-discard-intent", "rollback-discarded",
                        "rollback-restore-intent", "rollback-restored",
                    }
                ):
                    _restore_record(parents, record, journal=journal)

                # A disposal may have been registered by a previous recovery
                # or by cleanup just before an interruption.  It is private
                # and only removed through its exact recorded identity after
                # canonical A has been restored (or shown absent by policy).
                if record.disposal is not None or record.disposal_name is not None:
                    disposal = record.disposal
                    _finish_recovery_disposal(record, journal)
                    if disposal is not None and disposal in private_handles:
                        private_handles.remove(disposal)

                for private_name, private in (
                    ("staged", record.staged),
                    ("quarantine", record.quarantine),
                    ("rollback", record.rollback),
                ):
                    if private is None:
                        # The latest recovery frame can still name a private
                        # leaf which an earlier no-replace restore consumed
                        # (or which was already absent after a completed
                        # unlink/fsync boundary).  Canonical rollback has
                        # finished above; retire that stale private reference
                        # durably before allowing terminal journal cleanup.
                        name_attribute = private_name + "_name"
                        identity_attribute = private_name + "_identity"
                        if getattr(record, name_attribute) is not None:
                            setattr(record, name_attribute, None)
                            setattr(record, identity_attribute, None)
                            _journal_record_phase(journal, record, "cleanup-complete")
                        continue
                    _dispose_record_private(record, private, source=private_name, journal=journal)
                    private.close()
                    if private in private_handles:
                        private_handles.remove(private)
                    setattr(record, private_name, None)
                    if private_name == "staged":
                        record.staged_name = None
                        record.staged_identity = None
                    elif private_name == "quarantine":
                        record.quarantine_name = None
                        record.quarantine_identity = None
                    else:
                        record.rollback_name = None
                        record.rollback_identity = None
                    _journal_record_phase(journal, record, "cleanup-complete")

            parents.assert_current(phase="before restart rollback terminal frame")
            journal.persist(state="rolled-back")
            outcome = "rolled-back"

        parents.assert_current(phase="after restart recovery")
        recovery_receipt: PublicationTransactionReceipt | None = None
        if outcome == "rolled-back":
            # Keep the exact journal leaf live until an outer owner has had a
            # chance to persist the fresh identities.  This closes the
            # inverse-recovery window where B was restored with new inodes
            # but an outer receipt still named the superseded generation.
            recovery_receipt = _publication_transaction_receipt(
                journal,
                parents,
                records,
                state="recovered",
                postimages=_capture_recovered_postimages(parents, records),
            )
            if after_recovery is not None:
                after_recovery(recovery_receipt)
        if not journal.finalize_terminal():
            result: dict[str, object] = {
                "ok": True,
                "journal": str(path),
                "state": f"{outcome}-cleanup-deferred",
            }
            if recovery_receipt is not None:
                result["recovery_receipt"] = _publication_receipt_summary(recovery_receipt)
            return result
        result = {"ok": True, "journal": str(path), "state": outcome}
        if recovery_receipt is not None:
            result["recovery_receipt"] = _publication_receipt_summary(recovery_receipt)
        return result
    finally:
        if journal is not None:
            journal.close()
        for private in private_handles:
            private.close()
        parents.close()

def recover_publication_transaction(
    journal_path: Path | str | os.PathLike[str],
    *,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    """Public restart-recovery entry point for an interrupted publication."""

    return _recover_publication_transaction(journal_path, after_recovery=after_recovery)


def _install_publication_journal_path(layout: ReleaseLayout) -> Path:
    """Return the stable forward-install journal spanning wrappers and state."""

    return layout.root / _INSTALL_PUBLICATION_JOURNAL_NAME


def _rollback_publication_journal_path(layout: ReleaseLayout) -> Path:
    """Return the independent journal used by every B-to-A release inverse."""

    return layout.root / _ROLLBACK_PUBLICATION_JOURNAL_NAME


def _release_publication_journal_paths(layout: ReleaseLayout) -> tuple[Path, Path]:
    """Return every fixed journal path that can be owned by release-manager."""

    return (
        _install_publication_journal_path(layout),
        _rollback_publication_journal_path(layout),
    )


def _selected_release_publication_journal_path(
    layout: ReleaseLayout,
    journal_path: Path | str | os.PathLike[str],
) -> Path:
    """Accept only an exact current release-manager journal pathname.

    Outer activation receipts bind the inode as well as this pathname before
    calling the public recovery entry point.  Constraining the optional path
    to this small fixed set preserves that boundary while allowing a caller to
    recover the precise retained transaction when a terminal cleanup residue
    from the opposite direction is also present.
    """

    path = _canonical_publication_path(
        journal_path,
        label="managed release publication journal",
    )
    if path not in _release_publication_journal_paths(layout):
        raise ReleaseError("managed release publication journal is not owned by this layout")
    return path


def _recover_install_publication_locked(
    layout: ReleaseLayout,
    *,
    journal_path: Path | str | os.PathLike[str] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object] | None:
    if journal_path is not None:
        path = _selected_release_publication_journal_path(layout, journal_path)
        if not _path_lexists(path):
            if not _restore_disposed_publication_journal(path):
                return None
        return _recover_publication_transaction(path, after_recovery=after_recovery)

    for path in _release_publication_journal_paths(layout):
        if not _path_lexists(path):
            _restore_disposed_publication_journal(path)
    existing = [
        path for path in _release_publication_journal_paths(layout)
        if _path_lexists(path)
    ]
    if len(existing) > 1:
        # There is no trustworthy ordering field shared by independent
        # append-only journals.  Do not guess which transaction owns the
        # canonical leaves; callers with a durable callback receipt must pass
        # that exact path, while a human/operator can inspect and recover one
        # authenticated journal at a time.
        raise ReleaseError(
            "multiple managed release publication journals are present; "
            "recover an exact journal path"
        )
    if not existing:
        return None
    path = existing[0]
    if not _path_lexists(path):
        return None
    return _recover_publication_transaction(path, after_recovery=after_recovery)


def recover_install_publication(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    lock_timeout: float = 15.0,
    journal_path: Path | str | os.PathLike[str] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    """Recover a durable wrapper-plus-managed-state release journal.

    ``journal_path`` is optional for ordinary command-line/startup recovery.
    An outer transaction that retained a callback receipt should supply its
    exact callback path; it is accepted only when it is one of this layout's
    forward-install or inverse-rollback journals.  This is safe when
    ``state.json`` is absent: recovery authenticates the descriptor journal
    before deciding whether a first-install wrapper set is B to remove or an
    already terminal publication to clean up.

    ``after_recovery`` runs only after an inverse has restored its durable
    preimages.  It receives a receipt with ``state == "recovered"`` and exact
    descriptor-captured restored leaves in ``postimages`` while the source
    journal is still live.  Callback failure retains that journal for an
    idempotent retry instead of accepting a same-content replacement.
    """

    layout = release_layout(root=root, bin_dir=bin_dir)
    with release_lock(layout, timeout=lock_timeout):
        result = _recover_install_publication_locked(
            layout,
            journal_path=journal_path,
            after_recovery=after_recovery,
        )
    return result or {
        "ok": True,
        "recovered": False,
        "journal": str(
            _selected_release_publication_journal_path(layout, journal_path)
            if journal_path is not None
            else _install_publication_journal_path(layout)
        ),
    }


def _service_wrapper_api() -> object:
    """Load the managed-wrapper API without introducing an import cycle."""

    try:
        from . import service_wrappers
    except Exception as exc:
        raise ReleaseError(f"managed service-wrapper API is unavailable: {exc}") from exc
    return service_wrappers


def _release_requires_immutable_wrapper_execution(
    build_info: Mapping[str, object],
) -> bool:
    """Return whether this release must execute wrappers from its wheel site."""

    version = build_info.get("version")
    if not isinstance(version, str):
        return True
    match = _RELEASE_VERSION_PREFIX.fullmatch(version)
    if match is None:
        return True
    try:
        numeric = tuple(int(component) for component in match.groups())
    except ValueError:
        return True
    return numeric >= _IMMUTABLE_WRAPPER_EXECUTION_MIN_VERSION


def _release_wrapper_execution_kwargs(
    layout: ReleaseLayout,
    release_id: str,
) -> dict[str, object]:
    """Build the verified immutable import binding for one selected release.

    The release verification walks the frozen installed site and authenticates
    every RECORD member.  Wrapper rendering copies only the two modules it
    executes into a compact binding, then service_wrappers rechecks those
    exact single-linked files before importing them at runtime.
    """

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("wrapper execution release id is unsafe")
    release_dir = layout.releases_dir / release_id
    try:
        manifest = validate_release_manifest(
            read_json(release_dir / "manifest.json", label="release manifest")
        )
    except (BuildIdentityError, OSError, ValueError) as exc:
        raise ReleaseError(f"could not read wrapper execution release: {exc}") from exc
    build_info = manifest.get("build_info")
    if not isinstance(build_info, Mapping):
        raise ReleaseError("wrapper execution release has no build info")
    # Releases from before immutable wrapper execution never shipped the
    # service-wrapper module in their wheel.  Their wrappers are therefore
    # authenticated against the historical source-root contract, and their
    # extracted site is not an authority for wrapper rendering.  Decide that
    # compatibility path from the release identity before walking a possibly
    # damaged historical site.
    if not _release_requires_immutable_wrapper_execution(build_info):
        return {}
    verification, site_manifest = _verify_release_dir_details(layout, release_id)
    required = (
        "termdeck/__init__.py",
        "termdeck/service_wrappers.py",
        "termdeck/web_activation.py",
    )
    file_rows = {
        str(name): (digest, size)
        for name, digest, size in site_manifest["files"]
    }
    missing = [name for name in required if name not in file_rows]
    frozen = verification.get("frozen_directories") is True
    if missing or not frozen:
        detail = "missing " + ", ".join(missing) if missing else "site is not frozen"
        raise ReleaseError(f"managed release cannot bind immutable wrapper execution: {detail}")
    binding: dict[str, object] = {}
    for name in required:
        digest, size = file_rows[name]
        if not isinstance(digest, str) or isinstance(size, bool) or not isinstance(size, int):
            raise ReleaseError("managed release execution binding is malformed")
        binding[name] = {"sha256": digest, "size": size}
    return {
        "execution_root": release_dir / "site",
        "execution_binding": binding,
    }


def _wrapper_layout_kwargs(
    layout: ReleaseLayout,
    release_id: str | None = None,
) -> dict[str, object]:
    """Build the wrapper API layout for this release-manager root.

    Production uses the canonical Warp roots.  Disposable release-manager
    fixtures use sibling roots so their wrapper publication never reaches the
    live service directories.  The lexical root check is intentional: tests
    may patch production admission while still using a temporary root.
    """

    canonical = layout.root.resolve(strict=False) == _DEPLOYMENT_MANAGED_ROOT.resolve(
        strict=False
    )
    if canonical:
        source_root = _CANONICAL_SOURCE_ROOT
        runtime_root = _DEPLOYMENT_MANAGED_ROOT.parent
        state_root = _DEPLOYMENT_STATE_ROOT
        unit_dir = Path("/home/arch/.config/systemd/user")
        control_root = _CANONICAL_CONTROL_ROOT
    else:
        source_root = Path(__file__).resolve().parent.parent
        runtime_root = layout.root.parent
        state_root = layout.root.parent / "state"
        unit_dir = layout.root.parent / "units"
        # Tests and other disposable layouts must opt into an explicit
        # sibling control namespace.  It is intentionally not created here:
        # a same-UID fixture directory is API plumbing, never a substitute
        # for the production root-owned attestation boundary.
        control_root = layout.root.parent.with_name(
            layout.root.parent.name + "-termdeck-control"
        )
    result: dict[str, object] = {
        "source_root": source_root,
        "control_root": control_root,
        "runtime_root": runtime_root,
        "state_root": state_root,
        "managed_root": layout.root,
        "managed_bin": layout.bin_dir,
        "unit_dir": unit_dir,
        "manifest_path": layout.root / "wrapper-manifest.json",
    }
    if release_id is not None:
        result.update(_release_wrapper_execution_kwargs(layout, release_id))
    return result


def _wrapper_control_attestation_binding(wrapper_kwargs: Mapping[str, object]) -> dict[str, object]:
    """Return the immutable wrapper's public root-attestor protocol binding.

    The structure carries endpoint and schema metadata only.  Callers still
    need a response authenticated by the root attestor; it is never a
    credential or an authorization to sign arbitrary runtime receipts.
    """

    raw_root = wrapper_kwargs.get("control_root")
    if not isinstance(raw_root, (str, Path)):
        raise ReleaseError("managed service-wrapper control root is unavailable")
    root = Path(raw_root)
    return {
        "socket": str(root / _CONTROL_ATTEST_SOCKET_NAME),
        "operation": _CONTROL_ATTEST_OPERATION,
        "request_schema": _CONTROL_ATTEST_REQUEST_SCHEMA,
        "response_schema": _CONTROL_ATTEST_RESPONSE_SCHEMA,
        "producer_principal": _CONTROL_ATTEST_PRODUCER,
        "excluded_principal": _CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
        "control_root_owner": _CONTROL_ATTEST_OWNER,
        "control_root_group": _CONTROL_ATTEST_GROUP,
        "control_root_mode": _CONTROL_ROOT_MODE,
        "socket_owner": _CONTROL_ATTEST_OWNER,
        "socket_group": _CONTROL_ATTEST_GROUP,
        "socket_mode": _CONTROL_ATTEST_SOCKET_MODE,
    }


def release_wrapper_execution_binding(
    *,
    release_id: str,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    lock_timeout: float = 15.0,
) -> dict[str, object]:
    """Return a freshly verified immutable wrapper import binding.

    Consumers use this before reading an existing managed wrapper set.  The
    returned root and module rows come from the named release's verified site
    manifest rather than from a mutable wrapper manifest.  Legacy releases
    that predate the wheel-site modules report ``immutable=False``; current
    releases fail closed when either required module is missing or unfrozen.
    """

    if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("wrapper execution release id is invalid")
    layout = release_layout(root=root, bin_dir=bin_dir)
    with release_lock(layout, timeout=lock_timeout):
        wrapper_kwargs = _wrapper_layout_kwargs(layout, release_id)
        binding = {
            key: wrapper_kwargs[key]
            for key in ("execution_root", "execution_binding")
            if key in wrapper_kwargs
        }
        expected_attestation = _wrapper_control_attestation_binding(wrapper_kwargs)
        try:
            wrapper_api = _service_wrapper_api()
            observed_attestation = wrapper_api.control_attestation_binding(  # type: ignore[attr-defined]
                **wrapper_kwargs
            )
        except Exception as exc:
            raise ReleaseError(
                f"managed service-wrapper control attestation is invalid: {exc}"
            ) from exc
        if not isinstance(observed_attestation, Mapping) or dict(observed_attestation) != expected_attestation:
            raise ReleaseError("managed service-wrapper control attestation differs from policy")
    result: dict[str, object] = {
        "release_id": release_id,
        "immutable": bool(binding),
        "control_root": str(wrapper_kwargs["control_root"]),
        "control_socket": str(
            Path(str(wrapper_kwargs["control_root"])) / _CONTROL_ATTEST_SOCKET_NAME
        ),
        "control_attestation": expected_attestation,
    }
    result.update(binding)
    return result


def _wrapper_path_set(api: object, kwargs: Mapping[str, object], release_id: str) -> tuple[Path, ...]:
    """Return the complete path set rendered by the wrapper API."""

    try:
        rendered = api.render_wrappers(release_id, **dict(kwargs))  # type: ignore[attr-defined]
        all_files = rendered.all_files()  # type: ignore[attr-defined]
    except Exception as exc:
        raise ReleaseError(f"could not render managed service wrappers: {exc}") from exc
    if not isinstance(all_files, Mapping) or not all_files:
        raise ReleaseError("managed service-wrapper render returned no files")
    paths: list[Path] = []
    for raw_path in all_files:
        try:
            path = Path(raw_path)
        except TypeError as exc:
            raise ReleaseError("managed service-wrapper render returned an invalid path") from exc
        if not path.is_absolute() or path != Path(os.path.normpath(str(path))):
            raise ReleaseError(f"managed service-wrapper path is not canonical: {path}")
        paths.append(path)
    return tuple(sorted(set(paths), key=str))


def _wrapper_snapshot_item(item: object, *, path: Path) -> _AnchoredFileSnapshot:
    """Normalize a wrapper preimage without dropping its anchored identity."""

    try:
        return _expected_anchored_snapshot_from_object(
            item,
            label=f"managed service-wrapper preimage {path}",
        )
    except ReleaseError as exc:
        raise ReleaseError(f"managed service-wrapper preimage is invalid for {path}: {exc}") from exc


def _wrapper_snapshots_equal(
    left: object,
    right: object,
    paths: Sequence[Path],
) -> bool:
    try:
        for path in paths:
            left_item = left[path]  # type: ignore[index]
            right_item = right[path]  # type: ignore[index]
            if _wrapper_snapshot_item(left_item, path=path) != _wrapper_snapshot_item(
                right_item, path=path
            ):
                return False
    except (KeyError, TypeError, ReleaseError):
        return False
    try:
        return set(left) == set(paths) and set(right) == set(paths)  # type: ignore[arg-type]
    except TypeError:
        return False


def _wrapper_snapshot_payload(
    snapshot: object,
    *,
    kwargs: Mapping[str, object],
    paths: Sequence[Path],
) -> bytes:
    entries: list[dict[str, object]] = []
    for path in paths:
        try:
            item = snapshot[path]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise ReleaseError(f"managed service-wrapper snapshot is incomplete: {path}") from exc
        anchored = _wrapper_snapshot_item(item, path=path)
        file_snapshot = anchored.snapshot
        identity = anchored.identity
        if file_snapshot.exists and (identity is None or anchored.sha256 is None):
            raise ReleaseError(
                f"managed service-wrapper snapshot lost its anchored identity: {path}"
            )
        if not file_snapshot.exists and (identity is not None or anchored.sha256 is not None):
            raise ReleaseError(
                f"managed service-wrapper absent snapshot has an anchored identity: {path}"
            )
        entries.append(
            {
                "path": str(path),
                "exists": file_snapshot.exists,
                "content": base64.b64encode(file_snapshot.content).decode("ascii"),
                "mode": file_snapshot.mode,
                "device": identity.device if identity is not None else None,
                "inode": identity.inode if identity is not None else None,
                "nlink": identity.nlink if identity is not None else None,
                "size": identity.size if identity is not None else None,
                "mtime_ns": identity.mtime_ns if identity is not None else None,
                "ctime_ns": identity.ctime_ns if identity is not None else None,
                "sha256": anchored.sha256,
            }
        )
    layout_fields = {
        key: str(kwargs[key])
        for key in (
            "source_root",
            "control_root",
            "runtime_root",
            "state_root",
            "managed_root",
            "managed_bin",
            "unit_dir",
            "manifest_path",
        )
        if key in kwargs
    }
    return canonical_json_bytes(
        {
            "schema": _SERVICE_WRAPPER_PREIMAGE_SCHEMA,
            "layout": layout_fields,
            "paths": entries,
        }
    )


def _wrapper_snapshot_from_payload(
    content: bytes,
    *,
    kwargs: Mapping[str, object],
    paths: Sequence[Path],
) -> dict[Path, dict[str, object]]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("managed service-wrapper preimage is not valid JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"schema", "layout", "paths"}:
        raise ReleaseError("managed service-wrapper preimage fields differ from schema")
    if value.get("schema") != _SERVICE_WRAPPER_PREIMAGE_SCHEMA:
        raise ReleaseError("unsupported managed service-wrapper preimage schema")
    layout_value = value.get("layout")
    expected_layout = {
        key: str(kwargs[key])
        for key in (
            "source_root",
            "control_root",
            "runtime_root",
            "state_root",
            "managed_root",
            "managed_bin",
            "unit_dir",
            "manifest_path",
        )
        if key in kwargs
    }
    if layout_value != expected_layout:
        raise ReleaseError("managed service-wrapper preimage layout does not match")
    raw_entries = value.get("paths")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(paths):
        raise ReleaseError("managed service-wrapper preimage path set is incomplete")
    expected = {str(path) for path in paths}
    result: dict[Path, dict[str, object]] = {}
    legacy_fields = {"path", "exists", "content", "mode"}
    identity_fields = {"device", "inode", "nlink", "size", "mtime_ns", "ctime_ns", "sha256"}
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise ReleaseError("managed service-wrapper preimage entry is malformed")
        raw_fields = set(raw)
        if raw_fields != legacy_fields and raw_fields != legacy_fields | identity_fields:
            raise ReleaseError("managed service-wrapper preimage entry is malformed")
        raw_path = raw.get("path")
        if not isinstance(raw_path, str):
            raise ReleaseError("managed service-wrapper preimage contains an unexpected path")
        path = Path(raw_path)
        if raw_path not in expected or path in result:
            raise ReleaseError("managed service-wrapper preimage contains an unexpected path")
        if not isinstance(raw.get("exists"), bool):
            raise ReleaseError("managed service-wrapper preimage existence is invalid")
        encoded = raw.get("content")
        if not isinstance(encoded, str):
            raise ReleaseError("managed service-wrapper preimage bytes are invalid")
        try:
            decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as exc:
            raise ReleaseError("managed service-wrapper preimage bytes are not base64") from exc
        mode = raw.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
            raise ReleaseError("managed service-wrapper preimage mode is invalid")
        if not raw["exists"] and decoded:
            raise ReleaseError("managed service-wrapper absent preimage contains bytes")
        entry: dict[str, object] = {
            "exists": raw["exists"],
            "content": decoded,
            "mode": mode,
        }
        if raw_fields == legacy_fields | identity_fields:
            entry.update({name: raw[name] for name in identity_fields})
        # Validate and retain a complete anchored identity where the receipt
        # carries one.  A legacy content-only receipt remains readable for
        # recovery, but a fresh receipt may only be emitted from a complete
        # anchored capture and can never be mistaken for a partial proof.
        _wrapper_snapshot_item(entry, path=path)
        result[path] = entry
    if {str(path) for path in result} != expected:
        raise ReleaseError("managed service-wrapper preimage path set is incomplete")
    if content != canonical_json_bytes(value):
        raise ReleaseError("managed service-wrapper preimage is not canonical")
    return result


def _service_wrapper_manifest_path(kwargs: Mapping[str, object]) -> Path:
    raw = kwargs.get("manifest_path")
    if not isinstance(raw, (str, Path)):
        raise ReleaseError("managed service-wrapper manifest path is unavailable")
    return Path(raw)


def _render_service_wrapper_files(
    api: object,
    kwargs: Mapping[str, object],
    release_id: str,
) -> Mapping[Path, bytes]:
    try:
        rendered = api.render_wrappers(release_id, **dict(kwargs))  # type: ignore[attr-defined]
        files = rendered.all_files()  # type: ignore[attr-defined]
    except Exception as exc:
        raise ReleaseError(f"could not render managed service-wrapper files: {exc}") from exc
    if not isinstance(files, Mapping) or not files:
        raise ReleaseError("managed service-wrapper render returned no files")
    normalized: dict[Path, bytes] = {}
    for raw_path, content in files.items():
        path = Path(raw_path)
        if not path.is_absolute() or path != Path(os.path.normpath(str(path))):
            raise ReleaseError(f"managed service-wrapper path is not canonical: {path}")
        if not isinstance(content, bytes):
            raise ReleaseError(f"managed service-wrapper render returned invalid bytes: {path}")
        normalized[path] = content
    return normalized


def _wrapper_file_mode(api: object, path: Path) -> int:
    return 0o755 if path.name in set(getattr(api, "WRAPPER_NAMES", ())) else 0o600


def _wrapper_publication_needs_repair(
    snapshot: object,
    rendered_files: Mapping[Path, bytes],
    *,
    api: object,
    paths: Sequence[Path],
    manifest_path: Path,
) -> bool:
    """Report repairs for deterministic files and missing mutable configs."""

    wrapper_names = set(getattr(api, "WRAPPER_NAMES", ()))
    for path in paths:
        try:
            anchored = _wrapper_snapshot_item(snapshot[path], path=path)  # type: ignore[index]
        except (KeyError, TypeError, ReleaseError):
            return True
        exists = anchored.snapshot.exists
        content = anchored.snapshot.content
        mode = anchored.snapshot.mode
        if not exists:
            return True
        if mode != _wrapper_file_mode(api, path):
            return True
        if path.name in wrapper_names or path == manifest_path:
            if content != rendered_files[path]:
                return True
    return False


def _read_wrapper_manifest_bytes(path: Path) -> tuple[bytes, Mapping[str, object]]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"could not inspect managed service-wrapper manifest: {path}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ReleaseError(f"managed service-wrapper manifest is unsafe: {path}")
    try:
        content = _read_regular_file_bytes(
            path,
            label="managed service-wrapper manifest",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"managed service-wrapper manifest is invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise ReleaseError("managed service-wrapper manifest is not an object")
    if content != canonical_json_bytes(value):
        raise ReleaseError("managed service-wrapper manifest is not canonical")
    return content, value


def _verify_service_wrapper_prepublication(
    layout: ReleaseLayout,
    expected_release_id: str | None,
    *,
    api: object,
    kwargs: Mapping[str, object],
    paths: Sequence[Path],
) -> dict[str, object]:
    """Authenticate the current set while allowing missing files to be repaired.

    A missing destination is recoverable because the manifest and every
    remaining destination still bind the current release.  Existing bytes are
    checked against the deterministic render, so a changed destination fails
    closed instead of being silently repaired.
    """

    # Existing A wrappers can have a different immutable site root from the
    # B set we are about to render.  Their manifest must be checked against
    # A's verified binding, never re-rendered through B's import root.
    current_kwargs = (
        _wrapper_layout_kwargs(layout, expected_release_id)
        if expected_release_id is not None
        else dict(kwargs)
    )
    manifest_path = _service_wrapper_manifest_path(current_kwargs)
    present = {path for path in paths if _path_lexists(path)}
    if not _path_lexists(manifest_path):
        if present:
            raise ReleaseError(
                "managed service-wrapper destination has no authenticated manifest"
            )
        return {
            "ok": True,
            "release_id": None,
            "manifest_path": str(manifest_path),
            "managed": False,
            "wrapper_names": [],
        }
    if len(present) == len(paths):
        # Let the wrapper API perform its complete readback when no repair is
        # needed.  This also preserves its handling of live config identity
        # fields while keeping the missing-file repair path below narrow.
        return _verify_service_wrappers(
            layout,
            expected_release_id,
            api=api,
            kwargs=current_kwargs,
            expected_paths=paths,
        )
    manifest_bytes, manifest = _read_wrapper_manifest_bytes(manifest_path)
    actual_release = manifest.get("release_id")
    if not isinstance(actual_release, str) or not actual_release:
        raise ReleaseError("managed service-wrapper manifest release id is invalid")
    if expected_release_id is not None and actual_release != expected_release_id:
        raise ReleaseError("managed service-wrapper release id does not match activation")
    rendered_files = _render_service_wrapper_files(api, current_kwargs, actual_release)
    if set(rendered_files) != set(paths):
        raise ReleaseError("managed service-wrapper render path set changed")
    if manifest_bytes != rendered_files[manifest_path]:
        raise ReleaseError("managed service-wrapper manifest is not bound to its render")
    wrapper_names = set(getattr(api, "WRAPPER_NAMES", ()))
    for path in paths:
        if not _path_lexists(path):
            continue
        try:
            info = path.lstat()
            observed = path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"could not read managed service-wrapper destination: {path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError(f"managed service-wrapper destination is unsafe: {path}")
        if stat.S_IMODE(info.st_mode) != _wrapper_file_mode(api, path):
            raise ReleaseError(f"managed service-wrapper destination mode is invalid: {path}")
        if path.name in wrapper_names and observed != rendered_files[path]:
            raise ReleaseError(f"managed service-wrapper bytes are not canonical: {path}")
        if path.name not in wrapper_names and path != manifest_path:
            try:
                config = json.loads(observed.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseError(f"managed service-wrapper config is invalid: {path}") from exc
            if not isinstance(config, Mapping):
                raise ReleaseError(f"managed service-wrapper config is invalid: {path}")
            unit = config.get("unit")
            if config.get("schema") != getattr(api, "UNIT_CONFIG_SCHEMA", None):
                raise ReleaseError(f"managed service-wrapper config schema is invalid: {path}")
            if (
                config.get("release_id") != actual_release
                or not isinstance(unit, str)
                or path.name != f"{unit}.conf"
            ):
                raise ReleaseError(f"managed service-wrapper config identity is invalid: {path}")
    return {
        "ok": True,
        "release_id": actual_release,
        "manifest_path": str(manifest_path),
        "managed": True,
        "wrapper_names": list(getattr(api, "WRAPPER_NAMES", ())),
        "config_paths": [str(path) for path in paths if path.name not in wrapper_names and path != manifest_path],
    }


def _verify_service_wrappers(
    layout: ReleaseLayout,
    expected_release_id: str | None,
    *,
    api: object | None = None,
    kwargs: Mapping[str, object] | None = None,
    expected_paths: Sequence[Path] | None = None,
) -> dict[str, object]:
    """Verify wrapper files, manifest bytes, and release identity together."""

    wrapper_api = api or _service_wrapper_api()
    wrapper_kwargs = dict(
        kwargs or _wrapper_layout_kwargs(layout, expected_release_id)
    )
    release_for_paths = expected_release_id or ("0" * 64)
    paths = tuple(expected_paths or _wrapper_path_set(wrapper_api, wrapper_kwargs, release_for_paths))
    manifest_path = _service_wrapper_manifest_path(wrapper_kwargs)
    present = tuple(path for path in paths if _path_lexists(path))
    if not _path_lexists(manifest_path):
        if present:
            raise ReleaseError(
                "managed service-wrapper destination has no authenticated manifest"
            )
        return {
            "ok": True,
            "release_id": None,
            "manifest_path": str(manifest_path),
            "managed": False,
            "wrapper_names": [],
        }
    _read_wrapper_manifest_bytes(manifest_path)
    try:
        readback = wrapper_api.readback_wrappers(  # type: ignore[attr-defined]
            **wrapper_kwargs,
            **({"expected_release_id": expected_release_id} if expected_release_id else {}),
        )
    except Exception as exc:
        raise ReleaseError(f"managed service-wrapper verification failed: {exc}") from exc
    if not isinstance(readback, Mapping) or readback.get("ok") is not True:
        raise ReleaseError("managed service-wrapper readback did not succeed")
    actual_release = readback.get("release_id")
    if not isinstance(actual_release, str):
        raise ReleaseError("managed service-wrapper manifest release id is invalid")
    if expected_release_id is not None and actual_release != expected_release_id:
        raise ReleaseError("managed service-wrapper release id does not match activation")
    rendered_files = _render_service_wrapper_files(wrapper_api, wrapper_kwargs, actual_release)
    if set(rendered_files) != set(paths):
        raise ReleaseError("managed service-wrapper render path set changed")
    wrapper_names = set(getattr(wrapper_api, "WRAPPER_NAMES", ()))
    for raw_path, expected_bytes in rendered_files.items():
        path = Path(raw_path)
        # Config files carry live PID/argv identity and therefore may differ
        # from their initial deterministic template.  readback_wrappers has
        # already validated their schema, unit, release identity, and mode;
        # wrappers and the manifest remain byte-for-byte deterministic.
        if path.name not in wrapper_names and path != manifest_path:
            continue
        try:
            observed = path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"could not read managed service-wrapper destination: {path}") from exc
        if observed != expected_bytes:
            raise ReleaseError(f"managed service-wrapper bytes are not canonical: {path}")
    return {
        "ok": True,
        "release_id": actual_release,
        "manifest_path": str(manifest_path),
        "manifest_sha256": readback.get("manifest_sha256"),
        "managed": True,
        "wrapper_names": readback.get("wrapper_names", []),
        "config_paths": readback.get("config_paths", []),
        "execution_root": readback.get("execution_root"),
        "execution_binding": readback.get("execution_binding"),
        "control_root": readback.get("control_root"),
        "control_socket": readback.get("control_socket"),
        "control_attestation": readback.get("control_attestation"),
    }


def _ensure_wrapper_ancestors(kwargs: Mapping[str, object]) -> None:
    """Create fixture-only roots through descriptor-anchored parent walks."""

    probes: list[Path] = []
    for key in ("state_root", "unit_dir"):
        value = kwargs.get(key)
        if not isinstance(value, (str, Path)):
            raise ReleaseError(f"managed service-wrapper {key} is unavailable")
        path = Path(value)
        probes.append(path / ".termdeck-wrapper-parent-anchor")
    _capture_publication_parent_guards(probes, create_parents=True)


def _legacy_wrapper_preimage_path(layout: ReleaseLayout) -> Path:
    return layout.legacy_dir / _SERVICE_WRAPPER_PREIMAGE_NAME


def _first_install_preimage_path(layout: ReleaseLayout) -> Path:
    return layout.legacy_dir / _FIRST_INSTALL_PREIMAGE_NAME


def _activation_publication_paths(layout: ReleaseLayout) -> tuple[Path, ...]:
    """Return the small mutable activation set owned by one release install."""

    return (layout.bootstrap_path, layout.state_path, layout.launcher_path)


def _require_empty_wrapper_snapshot(
    snapshot: object,
    paths: Sequence[Path],
    *,
    label: str,
) -> None:
    """Require an explicit absent preimage for every managed wrapper target."""

    try:
        actual_paths = set(snapshot)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ReleaseError(f"{label} does not contain a wrapper snapshot") from exc
    if actual_paths != set(paths):
        raise ReleaseError(f"{label} does not cover the complete wrapper set")
    for path in paths:
        try:
            exists = _wrapper_snapshot_item(snapshot[path], path=path).snapshot.exists  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise ReleaseError(f"{label} is incomplete: {path}") from exc
        if exists:
            raise ReleaseError(f"{label} does not record an empty wrapper preimage")


def _prepare_first_install_preimage_publication(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    publication: _ServiceWrapperPublication,
    *,
    autostart_publication: _AutostartPublication | None = None,
) -> _FirstInstallPreimagePublication | None:
    """Capture an explicit empty baseline before the very first activation.

    A legacy launcher receipt is sufficient for a conventional upgrade.  When
    both the managed root and stable launcher begin empty, however, deleting
    their installed files later is safe only when that absence was recorded
    before B was published.  This receipt is intentionally created only for
    that unambiguous case.
    """

    if (
        state.get("current_release_id") is not None
        or state.get("previous_release_id") is not None
        or state.get("legacy_launcher") is not None
        or state.get("generation") != 0
        or publication.legacy_preimage_write is None
    ):
        return None
    if publication.legacy_preimage_before.snapshot.exists:
        raise ReleaseError("first-install wrapper preimage receipt was not absent")
    if autostart_publication is not None and (
        autostart_publication.descriptor_preimage.exists
        or autostart_publication.receipt_preimage.exists
    ):
        # An empty-legacy inverse may remove only paths whose absence is
        # explicit.  A pre-existing desktop descriptor has a different,
        # separately backed-up recovery contract, so do not silently fold it
        # into this empty baseline.
        return None
    _require_empty_wrapper_snapshot(
        publication.preimage,
        publication.expected_paths,
        label="managed service-wrapper first-install preimage",
    )
    activation_paths = _activation_publication_paths(layout)
    receipt_path = _first_install_preimage_path(layout)
    guarded_paths = (*activation_paths, publication.legacy_preimage_path, receipt_path)
    parent_guards = _capture_publication_parent_guards(
        guarded_paths,
        create_parents=True,
    )
    snapshots = _capture_files_safely(
        tuple((path, f"first-install preimage {path.name}") for path in activation_paths)
        + ((receipt_path, "first-install preimage receipt"),),
        parent_guards=parent_guards,
    )
    activation_preimages = {path: snapshots[path] for path in activation_paths}
    if any(snapshot.exists for snapshot in activation_preimages.values()):
        return None
    receipt_preimage = snapshots[receipt_path]
    if receipt_preimage.exists:
        raise ReleaseError("first-install preimage receipt is already present")
    return _FirstInstallPreimagePublication(
        receipt_path=receipt_path,
        receipt_preimage=receipt_preimage,
        activation_preimages=activation_preimages,
        parent_guards=parent_guards,
    )


def _first_install_preimage_payload(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    publication: _ServiceWrapperPublication,
    first_install: _FirstInstallPreimagePublication,
    *,
    autostart_publication: _AutostartPublication | None = None,
) -> bytes:
    """Serialize the immutable absent baseline bound to the selected B state."""

    current = state.get("current_release_id")
    if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
        raise ReleaseError("first-install preimage has no selected release")
    activation_paths = _activation_publication_paths(layout)
    if set(first_install.activation_preimages) != set(activation_paths):
        raise ReleaseError("first-install preimage does not cover activation destinations")
    for path in activation_paths:
        if first_install.activation_preimages[path].exists:
            raise ReleaseError("first-install preimage is not empty")
    wrapper_content = publication.legacy_preimage_write
    if not isinstance(wrapper_content, bytes):  # pragma: no cover - guarded by preparation
        raise ReleaseError("first-install wrapper preimage is unavailable")
    wrapper_snapshot = _wrapper_snapshot_from_payload(
        wrapper_content,
        kwargs=publication.kwargs,
        paths=publication.expected_paths,
    )
    _require_empty_wrapper_snapshot(
        wrapper_snapshot,
        publication.expected_paths,
        label="managed service-wrapper first-install receipt",
    )
    autostart_preimage: list[dict[str, object]] = []
    if autostart_publication is not None:
        if (
            autostart_publication.descriptor_preimage.exists
            or autostart_publication.receipt_preimage.exists
        ):
            raise ReleaseError("first-install autostart preimage is not empty")
        autostart_preimage = [
            {"path": str(autostart_publication.descriptor_path), "exists": False},
            {"path": str(autostart_publication.receipt_path), "exists": False},
        ]
    payload = {
        "schema": _FIRST_INSTALL_PREIMAGE_SCHEMA,
        "release_id": current,
        "state_sha256": sha256_bytes(canonical_json_bytes(state)),
        "activation_preimage": [
            {"path": str(path), "exists": False}
            for path in activation_paths
        ],
        # Both receipts are removed by an empty rollback.  Record their
        # absence explicitly so their later names cannot be mistaken for an
        # unrelated file that happened to share a managed directory.
        "receipt_preimage": [
            {"path": str(publication.legacy_preimage_path), "exists": False},
            {"path": str(first_install.receipt_path), "exists": False},
        ],
        "autostart_preimage": autostart_preimage,
        "wrapper_preimage_path": str(publication.legacy_preimage_path),
        "wrapper_preimage_sha256": sha256_bytes(wrapper_content),
    }
    return canonical_json_bytes(payload)


def _validate_first_install_preimage_payload(
    content: bytes,
    *,
    layout: ReleaseLayout,
    state: Mapping[str, object],
    wrapper_preimage_path: Path,
    autostart_paths: Sequence[Path] = (),
) -> str:
    """Validate a durable empty baseline and return its wrapper payload hash."""

    if len(content) > _MAX_FIRST_INSTALL_PREIMAGE_BYTES:
        raise ReleaseError("first-install preimage receipt is oversized")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("first-install preimage receipt is not valid JSON") from exc
    fields = {
        "schema",
        "release_id",
        "state_sha256",
        "activation_preimage",
        "receipt_preimage",
        "autostart_preimage",
        "wrapper_preimage_path",
        "wrapper_preimage_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReleaseError("first-install preimage receipt fields differ from schema")
    if value.get("schema") != _FIRST_INSTALL_PREIMAGE_SCHEMA:
        raise ReleaseError("first-install preimage receipt schema is unsupported")
    current = state.get("current_release_id")
    if (
        not isinstance(current, str)
        or _RELEASE_ID.fullmatch(current) is None
        or value.get("release_id") != current
    ):
        raise ReleaseError("first-install preimage receipt release does not match current B")
    expected_state_hash = sha256_bytes(canonical_json_bytes(state))
    if value.get("state_sha256") != expected_state_hash:
        raise ReleaseError("first-install preimage receipt state does not match current B")
    if value.get("wrapper_preimage_path") != str(wrapper_preimage_path):
        raise ReleaseError("first-install preimage wrapper path is invalid")
    wrapper_hash = value.get("wrapper_preimage_sha256")
    if not isinstance(wrapper_hash, str) or re.fullmatch(r"[0-9a-f]{64}", wrapper_hash) is None:
        raise ReleaseError("first-install preimage wrapper digest is invalid")
    entries = value.get("activation_preimage")
    paths = _activation_publication_paths(layout)
    if not isinstance(entries, list) or len(entries) != len(paths):
        raise ReleaseError("first-install preimage activation path set is incomplete")
    for raw, expected_path in zip(entries, paths):
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"path", "exists"}
            or raw.get("path") != str(expected_path)
            or raw.get("exists") is not False
        ):
            raise ReleaseError("first-install preimage does not explicitly record absence")
    receipt_entries = value.get("receipt_preimage")
    expected_receipt_paths = (wrapper_preimage_path, _first_install_preimage_path(layout))
    if not isinstance(receipt_entries, list) or len(receipt_entries) != len(expected_receipt_paths):
        raise ReleaseError("first-install preimage receipt path set is incomplete")
    for raw, expected_path in zip(receipt_entries, expected_receipt_paths):
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"path", "exists"}
            or raw.get("path") != str(expected_path)
            or raw.get("exists") is not False
        ):
            raise ReleaseError("first-install preimage does not explicitly record receipt absence")
    autostart_entries = value.get("autostart_preimage")
    if not isinstance(autostart_entries, list) or len(autostart_entries) != len(autostart_paths):
        raise ReleaseError("first-install preimage autostart path set is incomplete")
    for raw, expected_path in zip(autostart_entries, autostart_paths):
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"path", "exists"}
            or raw.get("path") != str(expected_path)
            or raw.get("exists") is not False
        ):
            raise ReleaseError("first-install preimage does not explicitly record autostart absence")
    if content != canonical_json_bytes(value):
        raise ReleaseError("first-install preimage receipt is not canonical")
    return wrapper_hash


def _prepare_service_wrapper_publication(
    layout: ReleaseLayout,
    *,
    current_release_id: str | None,
    target_release_id: str,
) -> _ServiceWrapperPublication:
    """Capture and authenticate current wrappers before rendering a target set."""

    api = _service_wrapper_api()
    kwargs = _wrapper_layout_kwargs(layout, target_release_id)
    _ensure_wrapper_ancestors(kwargs)
    paths = _wrapper_path_set(api, kwargs, target_release_id)
    backup_path = _legacy_wrapper_preimage_path(layout)
    # Carry these identities through every later wrapper write and rollback.
    # This is deliberately captured before rendering/publishing so a renamed
    # root can never become a new publication destination between phases.
    parent_guards = _capture_publication_parent_guards(
        (*paths, backup_path),
        create_parents=True,
    )
    _verify_service_wrapper_prepublication(
        layout,
        current_release_id,
        api=api,
        kwargs=kwargs,
        paths=paths,
    )
    try:
        snapshot = api.capture_wrapper_snapshot(  # type: ignore[attr-defined]
            **kwargs,
            _parent_guards=parent_guards,
        )
    except Exception as exc:
        raise ReleaseError(f"could not capture managed service-wrapper preimages: {exc}") from exc
    if not isinstance(snapshot, Mapping) or set(snapshot) != set(paths):
        raise ReleaseError("managed service-wrapper preimage does not cover all destinations")

    backup_before = _capture_anchored_files_safely(
        ((backup_path, "managed service-wrapper legacy preimage"),),
        parent_guards=parent_guards,
    )[backup_path]
    if backup_before.snapshot.exists:
        try:
                _wrapper_snapshot_from_payload(
                backup_before.snapshot.content,
                kwargs=kwargs,
                paths=paths,
            )
        except ReleaseError:
            raise
        backup_write = None
    else:
        backup_before = _AnchoredFileSnapshot(_FileSnapshot(False), None, None)
        backup_write = _wrapper_snapshot_payload(snapshot, kwargs=kwargs, paths=paths)

    # The publication is a repair only when at least one target file differs
    # from the authenticated current bytes (including a missing destination).
    try:
        rendered = api.render_wrappers(target_release_id, **kwargs)  # type: ignore[attr-defined]
        rendered_files = rendered.all_files()  # type: ignore[attr-defined]
        repaired = _wrapper_publication_needs_repair(
            snapshot,
            rendered_files,
            api=api,
            paths=paths,
            manifest_path=Path(str(kwargs["manifest_path"])),
        )
    except Exception:
        # The precise repair bit is informational.  Any rendering problem is
        # still fatal before release publication.
        raise
    try:
        plan = api.prepare_wrapper_publication(  # type: ignore[attr-defined]
            rendered,
            _parent_guards=parent_guards,
            _snapshot=snapshot,
        )
        plan_paths = {
            Path(path)
            for path, _content, _mode, _label in getattr(plan, "writes")
        }
        if plan_paths != set(paths):
            raise ReleaseError("managed service-wrapper plan path set is incomplete")
        if getattr(plan, "snapshot", None) is None or getattr(plan, "parent_guards", None) != parent_guards:
            raise ReleaseError("managed service-wrapper plan lost its anchored preimage")
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError(f"could not prepare managed service-wrapper publication: {exc}") from exc
    return _ServiceWrapperPublication(
        api=api,
        kwargs=kwargs,
        target_release_id=target_release_id,
        preimage=snapshot,
        legacy_preimage_path=backup_path,
        legacy_preimage_before=backup_before,
        legacy_preimage_write=backup_write,
        expected_paths=paths,
        parent_guards=parent_guards,
        plan=plan,
        repaired=repaired,
    )


def _wrapper_rendered_summary(
    value: Mapping[str, object] | None,
    *,
    fallback: Mapping[str, object],
    repaired: bool,
) -> dict[str, object]:
    summary = dict(value or fallback)
    summary.pop("snapshot", None)
    summary.pop("rendered", None)
    summary["repaired"] = repaired
    return summary


def _publish_service_wrappers(
    publication: _ServiceWrapperPublication,
) -> dict[str, object]:
    """Publish target wrappers and return a JSON-safe summary."""

    current_snapshot = None
    try:
        current_snapshot = publication.api.capture_wrapper_snapshot(  # type: ignore[attr-defined]
            **publication.kwargs,
            _parent_guards=publication.parent_guards,
        )
    except Exception as exc:
        raise ReleaseError(f"could not recheck managed service-wrapper preimages: {exc}") from exc
    if not _wrapper_snapshots_equal(
        current_snapshot,
        publication.preimage,
        publication.expected_paths,
    ):
        raise ReleaseError("managed service-wrapper destination changed before publication")
    backup_written = False
    try:
        if publication.legacy_preimage_write is not None:
            _publish_files_transactionally(
                (
                    (
                        publication.legacy_preimage_path,
                        publication.legacy_preimage_write,
                        0o600,
                        "managed service-wrapper legacy preimage",
                    ),
                ),
                parent_guards=publication.parent_guards,
                expected_preimages={
                    publication.legacy_preimage_path: publication.legacy_preimage_before,
                },
            )
            backup_written = True
        result = publication.api.publish_wrappers(  # type: ignore[attr-defined]
            str(publication.target_release_id),
            **publication.kwargs,
            _parent_guards=publication.parent_guards,
        )
        if not isinstance(result, Mapping):
            raise ReleaseError("managed service-wrapper publication returned an invalid result")
        verified = _verify_service_wrappers(
            release_layout(
                root=Path(str(publication.kwargs["managed_root"])),
                bin_dir=Path(str(publication.kwargs["managed_bin"])),
            ),
            publication.target_release_id,
            api=publication.api,
            kwargs=publication.kwargs,
            expected_paths=publication.expected_paths,
        )
        return _wrapper_rendered_summary(
            verified,
            fallback=result,
            repaired=publication.repaired,
        )
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            # The wrapper API guarantees its own transaction, but restoring the
            # captured snapshot here also covers a failure after its verify.
            publication.api.restore_wrappers(  # type: ignore[attr-defined]
                publication.preimage,
                **publication.kwargs,
                _parent_guards=publication.parent_guards,
            )
        except Exception as restore_exc:
            rollback_errors.append(f"wrapper bytes: {restore_exc}")
        if backup_written:
            try:
                _restore_file(
                    publication.legacy_preimage_path,
                    publication.legacy_preimage_before.snapshot,
                    label="managed service-wrapper legacy preimage",
                    parent_guards=publication.parent_guards,
                )
            except Exception as restore_exc:
                rollback_errors.append(f"wrapper legacy preimage: {restore_exc}")
        detail = f"managed service-wrapper publication failed: {exc}"
        if rollback_errors:
            detail += "; rollback was incomplete: " + "; ".join(rollback_errors)
        raise ReleaseError(detail) from exc


def _restore_service_wrappers(
    publication: _ServiceWrapperPublication,
    snapshot: object,
    *,
    restore_legacy_preimage_file: bool = False,
) -> None:
    """Restore wrapper bytes and (optionally) the legacy snapshot metadata."""

    errors: list[str] = []
    try:
        publication.api.restore_wrappers(  # type: ignore[attr-defined]
            snapshot,
            **publication.kwargs,
            _parent_guards=publication.parent_guards,
        )
    except Exception as exc:
        errors.append(f"wrapper bytes: {exc}")
    if restore_legacy_preimage_file and publication.legacy_preimage_write is not None:
        try:
            _restore_file(
                publication.legacy_preimage_path,
                publication.legacy_preimage_before.snapshot,
                label="managed service-wrapper legacy preimage",
                parent_guards=publication.parent_guards,
            )
        except Exception as exc:
            errors.append(f"wrapper legacy preimage: {exc}")
    if errors:
        raise ReleaseError("managed service-wrapper rollback was incomplete: " + "; ".join(errors))


def _merge_publication_parent_guards(*values: object | None) -> _PublicationParentGuards:
    """Combine independently captured anchors without accepting a new parent.

    Wrapper preflight and activation preflight deliberately happen at separate
    points in install planning.  A path common to both sets is meaningful: a
    different chain there proves an ancestor was replaced in the gap, rather
    than becoming a fresh destination for a combined write.
    """

    by_path: dict[Path, _DirectoryGuard] = {}
    for value in values:
        guards = _coerce_parent_guards(value)
        if guards is None:
            continue
        for guard in guards.guards:
            previous = by_path.get(guard.path)
            if previous is not None and previous != guard:
                raise ReleaseError(
                    f"publication parent identity changed between preflight phases: {guard.path}"
                )
            by_path[guard.path] = guard
    if not by_path:
        raise ReleaseError("combined publication has no parent identities")
    return _PublicationParentGuards(tuple(by_path[path] for path in sorted(by_path, key=str)))


def _publish_install_wrapper_and_activation(
    publication: _ServiceWrapperPublication,
    activation_writes: Sequence[tuple[Path, bytes | None, int, str]],
    *,
    verify: Callable[[], None],
    first_install_preimage: _FirstInstallPreimagePublication | None,
    activation_expected_preimages: Mapping[Path, _FileSnapshot] | None = None,
    wrapper_snapshot: object | None = None,
    wrapper_expected_release_id: str | None = None,
    journal_path: Path | None = None,
    before_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
    production_probe: bool = False,
) -> dict[str, object]:
    """Publish wrappers and the selecting state through one durable journal.

    The state file is appended last by the caller.  The generic transaction
    writes its complete preimage/postimage journal before the first wrapper
    rename, so a process death cannot leave a wrapper generation that has no
    authenticated managed-state owner.
    """

    if wrapper_snapshot is None:
        plan = publication.plan
        raw_plan_writes = getattr(plan, "writes", None)
        plan_snapshot = getattr(plan, "snapshot", None)
        plan_guards = getattr(plan, "parent_guards", None)
        plan_verify = getattr(plan, "verify", None)
        if (
            not isinstance(raw_plan_writes, tuple)
            or not isinstance(plan_snapshot, Mapping)
            or plan_guards != publication.parent_guards
            or not callable(plan_verify)
        ):
            raise ReleaseError("managed service-wrapper publication plan is invalid")

        wrapper_writes: list[tuple[Path, bytes | None, int, str]] = []
        for raw_path, content, mode, label in raw_plan_writes:
            path = _canonical_publication_path(raw_path, label="managed service-wrapper plan")
            if content is not None and not isinstance(content, bytes):
                raise ReleaseError("managed service-wrapper plan has invalid bytes")
            wrapper_writes.append((path, content, mode, label))
        if {path for path, _content, _mode, _label in wrapper_writes} != set(publication.expected_paths):
            raise ReleaseError("managed service-wrapper publication plan path set is incomplete")
        expected_wrapper_preimages: Mapping[Path, object] = plan_snapshot
        verified_release_id = publication.target_release_id
    else:
        # Conventional legacy rollback restores the exact wrapper snapshot
        # that was recorded before management began.  It cannot use
        # ``restore_wrappers`` as a second independent transaction: an
        # interrupt between that publication and state.json previously left
        # an unowned legacy wrapper generation.  Turn the authenticated
        # snapshot into the same descriptor-journaled write set instead.
        try:
            target_paths = set(wrapper_snapshot)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ReleaseError("legacy service-wrapper snapshot is invalid") from exc
        if target_paths != set(publication.expected_paths):
            raise ReleaseError("legacy service-wrapper snapshot is incomplete")
        wrapper_writes = []
        for path in publication.expected_paths:
            try:
                desired = _wrapper_snapshot_item(wrapper_snapshot[path], path=path)  # type: ignore[index]
            except (KeyError, TypeError, ReleaseError) as exc:
                raise ReleaseError(f"legacy service-wrapper snapshot is invalid: {path}") from exc
            wrapper_writes.append(
                (
                    path,
                    desired.snapshot.content if desired.snapshot.exists else None,
                    desired.snapshot.mode,
                    f"legacy wrapper {path.name}",
                )
            )
        expected_wrapper_preimages = publication.preimage  # type: ignore[assignment]
        verified_release_id = wrapper_expected_release_id

        def plan_verify() -> None:
            _verify_service_wrappers(
                release_layout(
                    root=Path(str(publication.kwargs["managed_root"])),
                    bin_dir=Path(str(publication.kwargs["managed_bin"])),
                ),
                verified_release_id,
                api=publication.api,
                kwargs=publication.kwargs,
                expected_paths=publication.expected_paths,
            )

    normalized_activation: list[tuple[Path, bytes | None, int, str]] = []
    for raw_path, content, mode, label in activation_writes:
        normalized_activation.append(
            (_canonical_publication_path(raw_path, label=label), content, mode, label)
        )
    activation_paths = tuple(path for path, _content, _mode, _label in normalized_activation)
    if first_install_preimage is not None:
        activation_guards = _merge_publication_parent_guards(
            first_install_preimage.parent_guards,
            _capture_publication_parent_guards(activation_paths, create_parents=True),
        )
    elif not activation_paths:
        activation_guards = publication.parent_guards
    else:
        activation_guards = _capture_publication_parent_guards(
            activation_paths,
            create_parents=True,
        )
    parent_guards = _merge_publication_parent_guards(
        publication.parent_guards,
        activation_guards,
    )
    activation_preimages = (
        _capture_anchored_files_safely(
            tuple((path, label) for path, _content, _mode, label in normalized_activation),
            parent_guards=parent_guards,
        )
        if normalized_activation
        else {}
    )
    if activation_expected_preimages is not None:
        for path, expected in activation_expected_preimages.items():
            observed = activation_preimages.get(path)
            if observed is None or observed.snapshot != expected:
                raise ReleaseError(f"{path.name} changed after activation preflight")
    if first_install_preimage is not None:
        expected_first: dict[Path, _FileSnapshot] = {
            **first_install_preimage.activation_preimages,
            first_install_preimage.receipt_path: first_install_preimage.receipt_preimage,
        }
        for path, expected in (activation_expected_preimages or {}).items():
            if path not in expected_first:
                if expected.exists:
                    raise ReleaseError("first-install additional activation preimage is not empty")
                expected_first[path] = expected
        if set(expected_first) != set(activation_paths):
            raise ReleaseError("first-install preimage does not cover combined activation publication")
        for path, expected in expected_first.items():
            if activation_preimages[path].snapshot != expected:
                raise ReleaseError(f"{path.name} changed after first-install preflight")

    writes: list[tuple[Path, bytes | None, int, str]] = []
    expected_preimages: dict[Path, object] = dict(expected_wrapper_preimages)
    if publication.legacy_preimage_write is not None:
        writes.append(
            (
                publication.legacy_preimage_path,
                publication.legacy_preimage_write,
                0o600,
                "managed service-wrapper legacy preimage",
            )
        )
        expected_preimages[publication.legacy_preimage_path] = publication.legacy_preimage_before
    writes.extend(wrapper_writes)
    writes.extend(normalized_activation)
    expected_preimages.update(activation_preimages)
    if len({path for path, _content, _mode, _label in writes}) != len(writes):
        raise ReleaseError("combined install publication repeats a destination")

    def verify_combined() -> None:
        plan_verify()
        verify()

    published_receipt: PublicationTransactionReceipt | None = None

    def capture_published(receipt: PublicationTransactionReceipt) -> None:
        nonlocal published_receipt
        if after_publication is not None:
            after_publication(receipt)
        published_receipt = receipt

    _publish_files_transactionally(
        tuple(writes),
        verify=verify_combined,
        parent_guards=parent_guards,
        expected_preimages=expected_preimages,
        journal_path=journal_path,
        before_publish=before_publication,
        after_publish=capture_published,
        after_recovery=after_recovery,
        production_probe=production_probe,
    )
    verified = _verify_service_wrappers(
        release_layout(
            root=Path(str(publication.kwargs["managed_root"])),
            bin_dir=Path(str(publication.kwargs["managed_bin"])),
        ),
        verified_release_id,
        api=publication.api,
        kwargs=publication.kwargs,
        expected_paths=publication.expected_paths,
    )
    summary = _wrapper_rendered_summary(
        verified,
        fallback={},
        repaired=publication.repaired,
    )
    if published_receipt is None:  # pragma: no cover - nonempty combined writes always callback
        raise ReleaseError("combined install publication did not produce an authenticated receipt")
    summary["publication_receipt"] = _publication_receipt_summary(published_receipt)
    return summary


def _load_legacy_wrapper_snapshot(
    publication: _ServiceWrapperPublication,
) -> object:
    path = publication.legacy_preimage_path
    snapshot = _capture_file(
        path,
        label="managed service-wrapper legacy preimage",
        parent_guards=publication.parent_guards,
    )
    if not snapshot.exists:
        # A root created by an older release-manager has no wrapper history;
        # the safe legacy target is an absent wrapper set.
        return {
            item: {"exists": False, "content": b"", "mode": 0}
            for item in publication.expected_paths
        }
    return _wrapper_snapshot_from_payload(
        snapshot.content,
        kwargs=publication.kwargs,
        paths=publication.expected_paths,
    )


def _publish_service_wrapper_snapshot(
    publication: _ServiceWrapperPublication,
    snapshot: object,
    *,
    expected_release_id: str | None,
) -> dict[str, object]:
    """Publish a previously captured wrapper set for a legacy rollback."""

    try:
        current = publication.api.capture_wrapper_snapshot(  # type: ignore[attr-defined]
            **publication.kwargs,
            _parent_guards=publication.parent_guards,
        )
        if not _wrapper_snapshots_equal(current, publication.preimage, publication.expected_paths):
            raise ReleaseError("managed service-wrapper destination changed before rollback")
        publication.api.restore_wrappers(  # type: ignore[attr-defined]
            snapshot,
            **publication.kwargs,
            _parent_guards=publication.parent_guards,
        )
        layout = release_layout(
            root=Path(str(publication.kwargs["managed_root"])),
            bin_dir=Path(str(publication.kwargs["managed_bin"])),
        )
        summary = _verify_service_wrappers(
            layout,
            expected_release_id,
            api=publication.api,
            kwargs=publication.kwargs,
            expected_paths=publication.expected_paths,
        )
        summary["repaired"] = not _wrapper_snapshots_equal(
            publication.preimage,
            snapshot,
            publication.expected_paths,
        )
        return summary
    except Exception as exc:
        try:
            _restore_service_wrappers(publication, publication.preimage)
        except Exception as restore_exc:
            raise ReleaseError(
                f"managed service-wrapper rollback failed: {exc}; {restore_exc}"
            ) from exc
        raise ReleaseError(f"managed service-wrapper rollback failed and was rolled back: {exc}") from exc


def _rollback_first_install_to_empty_legacy(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    publication: _ServiceWrapperPublication,
    *,
    manage_autostart: bool = False,
    after_remove: Callable[[], None] | None = None,
    journal_path: Path | None = None,
    before_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    """Undo one authenticated first install whose recorded preimages are empty.

    Unlike an ordinary legacy rollback, this path has no launcher bytes to
    restore.  It therefore deletes only the exact B files that its persisted
    first-install receipt and complete wrapper preimage prove were absent
    before publication.  One descriptor-anchored transaction owns both the
    removal and any rollback if verification fails.
    """

    if (
        state.get("legacy_launcher") is not None
        or state.get("previous_release_id") is not None
        or state.get("generation") != 1
    ):
        raise ReleaseError("empty legacy rollback has an ambiguous managed-state preimage")
    current = state.get("current_release_id")
    if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
        raise ReleaseError("empty legacy rollback has no authenticated current B")

    # Validate B before accepting its receipt as an authority.  This proves
    # the selected release, bootstrap, launcher, and complete wrapper set all
    # still agree rather than treating a state hash as self-authenticating.
    _verify_managed_activation(layout, state, verify_release=True)

    first_receipt_path = _first_install_preimage_path(layout)
    autostart_paths: tuple[Path, ...] = ()
    autostart_descriptor: bytes | None = None
    if manage_autostart:
        # Bind the external desktop pair to B before treating it as part of
        # the empty transaction.  The first-install receipt below also proves
        # both paths were absent before installation.
        _verify_production_autostart(layout, state)
        autostart_descriptor_path = _production_autostart_path()
        autostart_receipt_path = _production_autostart_receipt_path(layout)
        autostart_paths = (autostart_descriptor_path, autostart_receipt_path)
        autostart_descriptor = _canonical_production_autostart_descriptor(layout)
    # State is the externally visible selection pointer.  Remove its exact B
    # instance last, after every executable/config/receipt it selects has
    # been durably removed, so a crash cannot advertise an empty legacy state
    # while the transaction is still deleting B-owned files.
    removal_paths = (
        *publication.expected_paths,
        layout.bootstrap_path,
        layout.launcher_path,
        *autostart_paths,
        publication.legacy_preimage_path,
        first_receipt_path,
        layout.state_path,
    )
    parent_guards = _capture_publication_parent_guards(
        removal_paths,
        create_parents=False,
    )
    current_anchored_files = _capture_anchored_files_safely(
        tuple((path, f"empty legacy rollback {path.name}") for path in removal_paths),
        parent_guards=parent_guards,
    )
    current_files = {
        path: captured.snapshot for path, captured in current_anchored_files.items()
    }
    if any(not current_files[path].exists for path in removal_paths):
        raise ReleaseError("empty legacy rollback is missing a transaction-owned B destination")
    if current_files[first_receipt_path].mode != 0o600:
        raise ReleaseError("first-install preimage receipt is not private")
    if current_files[publication.legacy_preimage_path].mode != 0o600:
        raise ReleaseError("managed service-wrapper preimage receipt is not private")

    wrapper_hash = _validate_first_install_preimage_payload(
        current_files[first_receipt_path].content,
        layout=layout,
        state=state,
        wrapper_preimage_path=publication.legacy_preimage_path,
        autostart_paths=autostart_paths,
    )
    wrapper_content = current_files[publication.legacy_preimage_path].content
    if sha256_bytes(wrapper_content) != wrapper_hash:
        raise ReleaseError("first-install preimage wrapper digest does not match")
    wrapper_preimage = _wrapper_snapshot_from_payload(
        wrapper_content,
        kwargs=publication.kwargs,
        paths=publication.expected_paths,
    )
    _require_empty_wrapper_snapshot(
        wrapper_preimage,
        publication.expected_paths,
        label="managed service-wrapper first-install receipt",
    )

    current_wrappers = {
        path: current_anchored_files[path] for path in publication.expected_paths
    }
    if not _wrapper_snapshots_equal(
        current_wrappers,
        publication.preimage,
        publication.expected_paths,
    ):
        raise ReleaseError("managed service-wrapper destination changed before empty rollback")

    # The authenticated state derives B's canonical bytes, and the secure
    # snapshot above pins exactly the instances that the unlink transaction
    # will remove.  Refuse a same-name residue instead of guessing ownership.
    if sha256_bytes(current_files[layout.bootstrap_path].content) != state.get(
        "bootstrap_sha256"
    ):
        raise ReleaseError("managed bootstrap changed before empty rollback")
    if sha256_bytes(current_files[layout.launcher_path].content) != state.get(
        "launcher_sha256"
    ):
        raise ReleaseError("managed launcher changed before empty rollback")
    if current_files[layout.state_path].content != canonical_json_bytes(state):
        raise ReleaseError("managed state changed before empty rollback")
    if manage_autostart:
        assert autostart_descriptor is not None  # guarded above
        descriptor_path, receipt_path = autostart_paths
        descriptor_snapshot = current_files[descriptor_path]
        receipt_snapshot = current_files[receipt_path]
        if descriptor_snapshot.content != autostart_descriptor or descriptor_snapshot.mode != 0o644:
            raise ReleaseError("production autostart descriptor changed before empty rollback")
        if receipt_snapshot.mode != 0o600:
            raise ReleaseError("production autostart receipt is not private")
        try:
            receipt_value = json.loads(receipt_snapshot.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("production autostart receipt is invalid") from exc
        if receipt_snapshot.content != canonical_json_bytes(receipt_value):
            raise ReleaseError("production autostart receipt is not canonical")
        validated_autostart = _validate_autostart_receipt(
            receipt_value,
            layout=layout,
            state=state,
            descriptor_path=descriptor_path,
            descriptor=descriptor_snapshot.content,
        )
        if validated_autostart["prior_descriptor"] is not None:
            raise ReleaseError("empty legacy rollback autostart preimage is not empty")

    def verify_empty_publication() -> None:
        observed = _capture_files_safely(
            tuple((path, f"empty legacy rollback {path.name}") for path in removal_paths),
            parent_guards=parent_guards,
        )
        if any(snapshot.exists for snapshot in observed.values()):
            raise ReleaseError("empty legacy rollback left transaction-owned residue")
        if load_managed_state(layout) != _default_state():
            raise ReleaseError("empty legacy rollback did not restore the default state")
        if after_remove is not None:
            after_remove()

    published_receipt: PublicationTransactionReceipt | None = None

    def capture_published(receipt: PublicationTransactionReceipt) -> None:
        nonlocal published_receipt
        if after_publication is not None:
            after_publication(receipt)
        published_receipt = receipt

    _publish_files_transactionally(
        tuple(
            (path, None, current_files[path].mode, f"empty legacy rollback {path.name}")
            for path in removal_paths
        ),
        verify=verify_empty_publication,
        parent_guards=parent_guards,
        # Preserve the exact instances that were authenticated above.  Equal
        # bytes alone are not enough authority to remove a same-name file that
        # appeared between B verification and the unlink transaction.
        expected_preimages=current_anchored_files,
        journal_path=journal_path,
        before_publish=before_publication,
        after_publish=capture_published,
        after_recovery=after_recovery,
    )
    if published_receipt is None:  # pragma: no cover - removal set is nonempty
        raise ReleaseError("empty legacy rollback did not produce an authenticated receipt")
    return {
        "ok": True,
        "target": "legacy-empty",
        "previous_release_id": current,
        "state_generation": 0,
        "service_wrappers": {
            "ok": True,
            "release_id": None,
            "restored": True,
            "paths": [str(path) for path in publication.expected_paths],
            # Keep the nested result aligned with the normal combined
            # publication contract.  Callers which persist the callback
            # receipt use this duplicate to detect an accidental hand-off to
            # a different journal before accepting an empty first-install
            # inverse as their own outer rollback boundary.
            "publication_receipt": _publication_receipt_summary(published_receipt),
        },
        "publication_receipt": _publication_receipt_summary(published_receipt),
    }


def _regular_file_hash(
    path: Path,
    *,
    label: str,
    executable: bool = False,
) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReleaseError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseError(f"{label} is not a regular file: {path}")
    if executable:
        permissions = stat.S_IMODE(mode)
        if permissions & 0o100 == 0 or not os.access(path, os.X_OK):
            raise ReleaseError(f"{label} is not owner-executable: {path}")
        if permissions & 0o022:
            raise ReleaseError(f"{label} is group/world writable: {path}")
    try:
        return sha256_file(path)
    except OSError as exc:
        raise ReleaseError(f"could not hash {label}: {path}: {exc}") from exc


def _optional_regular_hash(
    path: Path,
    *,
    label: str,
    executable: bool = False,
) -> str | None:
    if not _path_lexists(path):
        return None
    return _regular_file_hash(path, label=label, executable=executable)


def _state_digest(path: Path) -> str | None:
    return _optional_regular_hash(path, label="managed state")


def _release_verification_path(layout: ReleaseLayout, release_id: str) -> Path:
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("release verification release id is unsafe")
    return layout.root / f"verification-{release_id}.json"


def _validate_release_verification_receipt(
    value: object,
    *,
    release_id: str,
    manifest_sha256: str,
    wheel_sha256: str,
    wheel_size: int,
    frozen_directories: bool | None = None,
    site_manifest_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReleaseError("release verification receipt is not an object")
    receipt = {str(key): item for key, item in value.items()}
    expected_fields = {
        "schema",
        "release_id",
        "manifest_sha256",
        "wheel_sha256",
        "wheel_size",
        "installed_members",
        "installed_directories",
        "site_directories",
        "frozen_directories",
        "site_manifest",
        "site_manifest_sha256",
        "method",
    }
    if set(receipt) != expected_fields:
        raise ReleaseError("release verification receipt fields differ from schema")
    if receipt["schema"] != _RELEASE_VERIFICATION_SCHEMA:
        raise ReleaseError("release verification receipt schema is unsupported")
    if receipt["release_id"] != release_id:
        raise ReleaseError("release verification receipt release id mismatch")
    for key, expected in (
        ("manifest_sha256", manifest_sha256),
        ("wheel_sha256", wheel_sha256),
    ):
        observed = receipt[key]
        if not isinstance(observed, str) or not re.fullmatch(r"[0-9a-f]{64}", observed):
            raise ReleaseError(f"release verification receipt {key} is invalid")
        if observed != expected:
            raise ReleaseError(f"release verification receipt {key} mismatch")
    if (
        isinstance(receipt["wheel_size"], bool)
        or not isinstance(receipt["wheel_size"], int)
        or receipt["wheel_size"] != wheel_size
    ):
        raise ReleaseError("release verification receipt wheel size mismatch")
    for key in ("installed_members", "installed_directories", "site_directories"):
        count = receipt[key]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ReleaseError(f"release verification receipt {key} is invalid")
    if receipt["installed_directories"] != receipt["site_directories"] + 2:
        raise ReleaseError("release verification receipt directory count mismatch")
    if not isinstance(receipt["frozen_directories"], bool):
        raise ReleaseError("release verification receipt frozen policy is invalid")
    if frozen_directories is not None and receipt["frozen_directories"] != frozen_directories:
        raise ReleaseError("release verification receipt frozen policy mismatch")
    site_manifest = _validate_site_manifest(
        receipt["site_manifest"],
        release_id=release_id,
        wheel_sha256=wheel_sha256,
        installed_members=int(receipt["installed_members"]),
        installed_directories=int(receipt["site_directories"]),
    )
    observed_site_manifest_sha256 = receipt["site_manifest_sha256"]
    if (
        not isinstance(observed_site_manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", observed_site_manifest_sha256) is None
    ):
        raise ReleaseError("release verification receipt site manifest digest is invalid")
    if observed_site_manifest_sha256 != sha256_bytes(canonical_json_bytes(site_manifest)):
        raise ReleaseError("release verification receipt site manifest digest mismatch")
    if (
        site_manifest_sha256 is not None
        and observed_site_manifest_sha256 != site_manifest_sha256
    ):
        raise ReleaseError("release verification receipt installed site mismatch")
    if receipt["method"] != "full-records-site-manifest-v2":
        raise ReleaseError("release verification receipt method is unsupported")
    return receipt


def _read_release_verification_receipt(
    layout: ReleaseLayout,
    release_id: str,
    *,
    manifest: Mapping[str, object],
    manifest_bytes: bytes,
    site_manifest_sha256: str | None = None,
) -> dict[str, object]:
    wheel_sha256 = manifest.get("wheel_sha256")
    wheel_size = manifest.get("wheel_size")
    if (
        not isinstance(wheel_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", wheel_sha256) is None
        or isinstance(wheel_size, bool)
        or not isinstance(wheel_size, int)
        or wheel_size <= 0
    ):
        raise ReleaseError("release verification manifest has invalid wheel identity")
    receipt_path = _release_verification_path(layout, release_id)
    try:
        receipt_stat = receipt_path.lstat()
    except OSError as exc:
        raise ReleaseError(f"could not inspect release verification receipt: {exc}") from exc
    if (
        stat.S_ISLNK(receipt_stat.st_mode)
        or not stat.S_ISREG(receipt_stat.st_mode)
        or stat.S_IMODE(receipt_stat.st_mode) & 0o222
        or receipt_stat.st_nlink != 1
    ):
        raise ReleaseError("release verification receipt is writable, linked, or unsafe")
    receipt_bytes = _read_regular_file_bytes(
        receipt_path,
        label="release verification receipt",
        maximum_bytes=_MAX_RELEASE_VERIFICATION_BYTES,
    )
    try:
        value = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"release verification receipt is invalid: {exc}") from exc
    if receipt_bytes != canonical_json_bytes(value):
        raise ReleaseError("release verification receipt is not canonical")
    return _validate_release_verification_receipt(
        value,
        release_id=release_id,
        manifest_sha256=sha256_bytes(manifest_bytes),
        wheel_sha256=wheel_sha256,
        wheel_size=wheel_size,
        frozen_directories=_release_requires_frozen_directories(manifest["build_info"]),
        site_manifest_sha256=site_manifest_sha256,
    )


def _write_release_verification_receipt(
    layout: ReleaseLayout,
    release_id: str,
    release_dir: Path,
    verification: Mapping[str, object] | None = None,
    site_manifest: Mapping[str, object] | None = None,
    *,
    expected_release_guard: _DirectoryGuard | None = None,
) -> dict[str, object]:
    """Persist the result of one complete install-time tree verification.

    Launchers consume this immutable, release-bound receipt for a bounded,
    descriptor-directed site check instead of an unbounded recursive walk. The
    full verifier remains authoritative for install, explicit verify, and rollback.
    Retain the exact final directory ancestry throughout receipt publication;
    an already valid matching receipt is reused without replacing its inode.
    """

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("release verification release id is unsafe")
    expected_release = _canonical_publication_path(
        layout.releases_dir / release_id,
        label="release verification target",
    )
    if _canonical_publication_path(
        release_dir, label="release verification target"
    ) != expected_release:
        raise ReleaseError("release verification target is not the exact managed release")
    release_anchor = _open_directory_anchor(
        expected_release,
        label="release verification target",
        create_parents=False,
    )
    receipt_anchor: _DirectoryAnchor | None = None
    try:
        if expected_release_guard is not None and release_anchor.guard != expected_release_guard:
            raise ReleaseError("release verification target identity changed after verification")
        release_anchor.assert_current(phase="before release verification manifest read")
        manifest_identity = _validate_regular_at(
            release_anchor, "manifest.json", label="release verification manifest"
        )
        manifest_bytes = _read_immutable_file_at(
            release_anchor.descriptor,
            "manifest.json",
            label="release verification manifest",
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        try:
            manifest = validate_release_manifest(json.loads(manifest_bytes.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, BuildIdentityError) as exc:
            raise ReleaseError(f"release verification manifest is invalid: {exc}") from exc
        if manifest_bytes != canonical_json_bytes(manifest):
            raise ReleaseError("release verification manifest is not canonical")
        if manifest["release_id"] != release_id:
            raise ReleaseError("release verification manifest release id mismatch")
        if verification is None or site_manifest is None:
            verification, site_manifest = _verify_installed_tree_details(expected_release)
        release_anchor.assert_current(phase="after full release verification")
        validated_site_manifest = _validate_site_manifest(
            site_manifest,
            release_id=release_id,
            wheel_sha256=str(manifest["wheel_sha256"]),
            installed_members=int(verification.get("installed_members", 0)),
        )
        site_manifest_sha256 = sha256_bytes(canonical_json_bytes(validated_site_manifest))
        if verification.get("site_manifest_sha256") != site_manifest_sha256:
            raise ReleaseError("release verification site manifest changed after full verification")
        receipt = _validate_release_verification_receipt(
            {
                "schema": _RELEASE_VERIFICATION_SCHEMA,
                "release_id": release_id,
                "manifest_sha256": sha256_bytes(manifest_bytes),
                "wheel_sha256": manifest["wheel_sha256"],
                "wheel_size": manifest["wheel_size"],
                "installed_members": verification.get("installed_members"),
                "installed_directories": verification.get("installed_directories"),
                "site_directories": len(validated_site_manifest["directories"]),
                "frozen_directories": verification.get("frozen_directories"),
                "site_manifest": validated_site_manifest,
                "site_manifest_sha256": site_manifest_sha256,
                "method": "full-records-site-manifest-v2",
            },
            release_id=release_id,
            manifest_sha256=sha256_bytes(manifest_bytes),
            wheel_sha256=str(manifest["wheel_sha256"]),
            wheel_size=int(manifest["wheel_size"]),
            frozen_directories=_release_requires_frozen_directories(manifest["build_info"]),
            site_manifest_sha256=site_manifest_sha256,
        )
        receipt_bytes = canonical_json_bytes(receipt)
        receipt_path = _release_verification_path(layout, release_id)
        receipt_anchor = _open_directory_anchor(
            receipt_path.parent,
            label="release verification receipt parent",
            create_parents=False,
        )
        if (
            receipt_anchor.path != expected_release.parent.parent
            or receipt_anchor.chain != release_anchor.chain[:-2]
        ):
            raise ReleaseError("release verification receipt parent identity changed")

        def assert_current(*, phase: str) -> None:
            release_anchor.assert_current(phase=phase)
            receipt_anchor.assert_current(phase=phase)
            if _validate_regular_at(
                release_anchor, "manifest.json", label="release verification manifest"
            ) != manifest_identity:
                raise ReleaseError("release verification manifest changed after verification")

        def verify_receipt() -> None:
            assert_current(phase="before release verification receipt read")
            before = _validate_regular_at(
                receipt_anchor, receipt_path.name, label="release verification receipt"
            )
            observed = _read_immutable_file_at(
                receipt_anchor.descriptor,
                receipt_path.name,
                label="release verification receipt",
                maximum_bytes=_MAX_RELEASE_VERIFICATION_BYTES,
            )
            if observed != receipt_bytes:
                raise ReleaseError("existing release verification receipt differs from verified release")
            if _validate_regular_at(
                receipt_anchor, receipt_path.name, label="release verification receipt"
            ) != before:
                raise ReleaseError("release verification receipt changed while being read")
            assert_current(phase="after release verification receipt read")

        assert_current(phase="before release verification receipt publication")
        if _validate_regular_at(
            receipt_anchor, receipt_path.name, label="release verification receipt"
        ) is not None:
            # An immutable matching receipt is already authoritative.  Leave
            # its exact inode intact on reinstall and rollback.
            verify_receipt()
            return receipt
        _publish_files_transactionally(
            ((receipt_path, receipt_bytes, 0o444, "release verification receipt"),),
            verify=verify_receipt,
            before_publish=lambda _receipt: assert_current(
                phase="immediately before release verification receipt publication"
            ),
            parent_guards=_PublicationParentGuards((receipt_anchor.guard,)),
            expected_preimages={
                receipt_path: _AnchoredFileSnapshot(_FileSnapshot(False), None)
            },
        )
        assert_current(phase="after release verification receipt publication")
        return receipt
    finally:
        if receipt_anchor is not None:
            receipt_anchor.close()
        release_anchor.close()


def _release_requires_frozen_directories(
    build_info: Mapping[str, object],
) -> bool:
    """Gate enforced directory immutability without breaking legacy rollback.

    Releases before 0.2.13 were published with read-only files inside ordinary
    owner-writable directories. They remain valid rollback targets. Starting at
    0.2.13, every directory must be frozen to mode 0555. Unknown version syntax
    fails closed into the stricter policy so a future build cannot accidentally
    opt out of immutability merely by changing its version spelling.
    """

    version = build_info.get("version")
    if not isinstance(version, str):
        return True
    match = _RELEASE_VERSION_PREFIX.fullmatch(version)
    if match is None:
        return True
    try:
        numeric = tuple(int(component) for component in match.groups())
    except ValueError:
        return True
    return numeric >= _FROZEN_RELEASE_MIN_VERSION


def _default_state() -> dict[str, object]:
    return {
        "schema": MANAGED_STATE_SCHEMA,
        "generation": 0,
        "current_release_id": None,
        "previous_release_id": None,
        "manifest_sha256": "",
        "python_executable": "",
        "bootstrap_sha256": "",
        "launcher_path": "",
        "launcher_sha256": "",
        "legacy_launcher": None,
    }


def validate_managed_state(value: object) -> dict[str, object]:
    try:
        return validate_managed_state_identity(value)
    except BuildIdentityError as exc:
        raise ReleaseError(str(exc)) from exc


def load_managed_state(layout: ReleaseLayout) -> dict[str, object]:
    if not _path_lexists(layout.state_path):
        return _default_state()
    _regular_file_hash(layout.state_path, label="managed state")
    raw = layout.state_path.read_bytes()
    try:
        state = validate_managed_state(read_json(layout.state_path, label="managed state"))
    except BuildIdentityError as exc:
        raise ReleaseError(str(exc)) from exc
    if raw != canonical_json_bytes(state):
        raise ReleaseError("managed state is not canonically serialized")
    return state


@contextmanager
def release_lock(layout: ReleaseLayout, *, timeout: float = 15.0) -> Iterator[None]:
    layout.root.mkdir(parents=True, exist_ok=True)
    root_mode = layout.root.lstat().st_mode
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ReleaseError("managed release root is not a real directory")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(layout.lock_path, flags, 0o600)
    except OSError as exc:
        raise ReleaseError(f"could not open managed-release lock safely: {exc}") from exc
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise ReleaseError("managed-release lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReleaseError("timed out waiting for the managed-release lock")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _extract_verified_wheel(
    verified: VerifiedWheel,
    site: Path,
    *,
    content: bytes | None = None,
) -> None:
    site.mkdir(parents=True, exist_ok=False)
    wheel_content = content if content is not None else _verified_wheel_bytes(verified)
    if len(wheel_content) != verified.size or sha256_bytes(wheel_content) != verified.sha256:
        raise ReleaseError("wheel bytes do not match the verification receipt")
    with zipfile.ZipFile(io.BytesIO(wheel_content), "r") as archive:
        infos = _validate_wheel_members(archive)
        for info in infos:
            name = _safe_member_name(info.filename)
            target = site.joinpath(*PurePosixPath(name).parts)
            try:
                target.resolve(strict=False).relative_to(site.resolve())
            except ValueError as exc:
                raise ReleaseError(f"wheel extraction escaped destination: {name}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.read(info)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            mode = _wheel_member_mode(info)
            target.chmod(0o555 if mode & 0o111 else 0o444)
        _fsync_tree(site)


def _fsync_tree(root: Path) -> None:
    """Durably flush regular files and directories below ``root``."""

    directories: list[Path] = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in names:
            path = directory / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReleaseError(f"release tree contains a non-directory entry: {path}")
        for name in files:
            path = directory / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ReleaseError(f"release tree contains a symlink or special file: {path}")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ReleaseError(f"release tree file changed type while flushing: {path}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _freeze_release_tree(root: Path) -> int:
    """Freeze one unique staging tree to read/execute-only directories.

    All release files are already immutable at this point. Directory write
    permission is the remaining same-UID mutation capability because Python or a
    verifier could otherwise add ``__pycache__`` or arbitrary files. Validation
    happens before chmod, then directories are frozen bottom-up so no partially
    inspected path becomes the immutable published tree.
    """

    root_mode = root.lstat().st_mode
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ReleaseError("staging release root is not a real directory")
    resolved_root = root.resolve(strict=True)
    directories: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise ReleaseError(f"could not inspect staging release tree: {error}")

    for current, names, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory = Path(current)
        directory_mode = directory.lstat().st_mode
        if stat.S_ISLNK(directory_mode) or not stat.S_ISDIR(directory_mode):
            raise ReleaseError(f"staging release contains an unsafe directory: {directory}")
        try:
            directory.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise ReleaseError(f"staging release directory escaped root: {directory}") from exc
        directories.append(directory)
        for name in names:
            child = directory / name
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode) or not stat.S_ISDIR(child_mode):
                raise ReleaseError(f"staging release contains an unsafe directory: {child}")
        for name in files:
            path = directory / name
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                raise ReleaseError(f"staging release contains an unsafe file: {path}")
            if stat.S_IMODE(entry.st_mode) & 0o222 or entry.st_nlink != 1:
                raise ReleaseError(f"staging release file is writable or hard-linked: {path}")

    for directory in reversed(directories):
        directory.chmod(0o555)
    _fsync_tree(root)
    return len(directories)


def _verify_release_directories(root: Path, *, frozen: bool) -> int:
    """Verify real in-root directories and, for new releases, exact mode 0555."""

    resolved_root = root.resolve(strict=True)
    count = 0

    def walk_error(error: OSError) -> None:
        raise ReleaseError(f"could not inspect installed release directories: {error}")

    for current, directories, _files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory = Path(current)
        entry_mode = directory.lstat().st_mode
        if stat.S_ISLNK(entry_mode) or not stat.S_ISDIR(entry_mode):
            raise ReleaseError(f"installed release contains an unsafe directory: {directory}")
        try:
            directory.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise ReleaseError(f"installed release directory escaped root: {directory}") from exc
        if frozen and stat.S_IMODE(entry_mode) != 0o555:
            raise ReleaseError(f"installed frozen directory is writable or has wrong mode: {directory}")
        count += 1
        if count > _MAX_SITE_DIRECTORIES + 2:
            raise ReleaseError("installed release directory count is unsafe")
        for name in directories:
            child = directory / name
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode) or not stat.S_ISDIR(child_mode):
                raise ReleaseError(f"installed release contains an unsafe directory: {child}")
    return count


def _exact_directory_names(
    path: Path,
    *,
    label: str,
    expected: set[str],
) -> None:
    names: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) > len(expected):
                    raise ReleaseError(f"{label} contains unexpected entries")
    except OSError as exc:
        raise ReleaseError(f"could not enumerate {label}: {path}: {exc}") from exc
    if names != expected:
        raise ReleaseError(
            f"{label} entries differ from the immutable release envelope: "
            f"missing={sorted(expected - names)}, extra={sorted(names - expected)}"
        )


def _verify_release_envelope(
    root: Path,
    *,
    wheel_name: str,
) -> Path:
    _exact_directory_names(
        root,
        label="installed release root",
        expected={"site", "artifacts", "manifest.json"},
    )
    artifacts = root / "artifacts"
    artifacts_mode = artifacts.lstat().st_mode
    if stat.S_ISLNK(artifacts_mode) or not stat.S_ISDIR(artifacts_mode):
        raise ReleaseError("installed release artifacts is not a real directory")
    _exact_directory_names(
        artifacts,
        label="installed release artifacts",
        expected={wheel_name},
    )
    return artifacts / wheel_name


def _installed_site_entries(site: Path) -> tuple[set[str], set[str]]:
    """Return exact regular-file and directory sets after a full safe walk."""

    mode = site.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseError("installed release site is not a real directory")
    root = site.resolve(strict=True)
    files: set[str] = set()
    site_directories: set[str] = {"."}
    for current, child_directories, names in os.walk(
        site, topdown=True, followlinks=False
    ):
        directory = Path(current)
        try:
            directory.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReleaseError(f"installed release directory escaped site: {directory}") from exc
        relative_directory = directory.relative_to(site).as_posix()
        if relative_directory != ".":
            site_directories.add(relative_directory)
        for name in child_directories:
            path = directory / name
            entry_mode = path.lstat().st_mode
            if stat.S_ISLNK(entry_mode) or not stat.S_ISDIR(entry_mode):
                raise ReleaseError(f"installed release contains an unsafe directory: {path}")
        for name in names:
            path = directory / name
            entry_stat = path.lstat()
            entry_mode = entry_stat.st_mode
            if stat.S_ISLNK(entry_mode) or not stat.S_ISREG(entry_mode):
                raise ReleaseError(f"installed release contains a symlink or special file: {path}")
            if stat.S_IMODE(entry_mode) & 0o222:
                raise ReleaseError(f"installed release file is writable: {path}")
            if entry_stat.st_nlink != 1:
                raise ReleaseError(f"installed release file has multiple hard links: {path}")
            try:
                path.resolve(strict=True).relative_to(root)
            except ValueError as exc:
                raise ReleaseError(f"installed release file escaped site: {path}") from exc
            files.add(path.relative_to(site).as_posix())
    return files, site_directories


def _read_installed_member(site: Path, name: str) -> bytes:
    path = site.joinpath(*PurePosixPath(name).parts)
    entry_stat = path.lstat()
    mode = entry_stat.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseError(f"installed wheel member is not a regular file: {name}")
    if stat.S_IMODE(mode) & 0o222:
        raise ReleaseError(f"installed wheel member is writable: {name}")
    if entry_stat.st_nlink != 1:
        raise ReleaseError(f"installed wheel member has multiple hard links: {name}")
    try:
        path.resolve(strict=True).relative_to(site.resolve(strict=True))
    except ValueError as exc:
        raise ReleaseError(f"installed wheel member escaped site: {name}") from exc
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReleaseError(f"installed wheel member changed type while reading: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _record_digest_hex(value: str, *, name: str) -> str:
    match = re.fullmatch(r"sha256=([A-Za-z0-9_-]{43})", value)
    if match is None:
        raise ReleaseError(f"installed RECORD SHA-256 is invalid: {name}")
    try:
        decoded = base64.urlsafe_b64decode(match.group(1) + "=")
    except ValueError as exc:
        raise ReleaseError(f"installed RECORD SHA-256 is invalid: {name}") from exc
    if len(decoded) != hashlib.sha256().digest_size:
        raise ReleaseError(f"installed RECORD SHA-256 is invalid: {name}")
    return decoded.hex()


def _validate_site_manifest(
    value: object,
    *,
    release_id: str,
    wheel_sha256: str,
    installed_members: int | None = None,
    installed_directories: int | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReleaseError("installed site manifest is not an object")
    manifest = {str(key): item for key, item in value.items()}
    expected_fields = {
        "schema",
        "release_id",
        "wheel_sha256",
        "directories",
        "files",
    }
    if set(manifest) != expected_fields:
        raise ReleaseError("installed site manifest fields differ from schema")
    if manifest["schema"] != _SITE_MANIFEST_SCHEMA:
        raise ReleaseError("installed site manifest schema is unsupported")
    if manifest["release_id"] != release_id:
        raise ReleaseError("installed site manifest release id mismatch")
    if manifest["wheel_sha256"] != wheel_sha256:
        raise ReleaseError("installed site manifest wheel mismatch")

    directory_values = manifest["directories"]
    if not isinstance(directory_values, list):
        raise ReleaseError("installed site manifest directories are invalid")
    if not directory_values or len(directory_values) > _MAX_SITE_DIRECTORIES:
        raise ReleaseError("installed site manifest directory count is unsafe")
    if any(not isinstance(item, str) for item in directory_values):
        raise ReleaseError("installed site manifest directory path is invalid")
    directory_names = list(directory_values)
    if directory_names != sorted(set(directory_names)) or "." not in directory_names:
        raise ReleaseError("installed site manifest directories are not canonical")
    directory_set = set(directory_names)
    for name in directory_names:
        if name == ".":
            continue
        _safe_member_name(name)
        parent = PurePosixPath(name).parent.as_posix()
        if parent not in directory_set:
            raise ReleaseError("installed site manifest directory parent is missing")

    file_values = manifest["files"]
    if not isinstance(file_values, list):
        raise ReleaseError("installed site manifest files are invalid")
    if not file_values or len(file_values) > _MAX_WHEEL_ENTRIES:
        raise ReleaseError("installed site manifest file count is unsafe")
    normalized_files: list[list[object]] = []
    total_size = 0
    for row in file_values:
        if not isinstance(row, list) or len(row) != 3:
            raise ReleaseError("installed site manifest file row is invalid")
        name, digest, size = row
        if not isinstance(name, str):
            raise ReleaseError("installed site manifest file path is invalid")
        _safe_member_name(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReleaseError(f"installed site manifest digest is invalid: {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseError(f"installed site manifest size is invalid: {name}")
        parent = PurePosixPath(name).parent.as_posix()
        if parent not in directory_set:
            raise ReleaseError(f"installed site manifest file parent is missing: {name}")
        total_size += size
        if total_size > _MAX_WHEEL_TOTAL_BYTES:
            raise ReleaseError("installed site manifest total size is unsafe")
        normalized_files.append([name, digest, size])
    if normalized_files != sorted(normalized_files, key=lambda row: str(row[0])):
        raise ReleaseError("installed site manifest files are not sorted")
    file_names = [str(row[0]) for row in normalized_files]
    if len(file_names) != len(set(file_names)):
        raise ReleaseError("installed site manifest file paths are duplicated")
    if installed_members is not None and len(file_names) != installed_members:
        raise ReleaseError("installed site manifest member count mismatch")
    if installed_directories is not None and len(directory_names) != installed_directories:
        raise ReleaseError("installed site manifest directory count mismatch")
    if len(canonical_json_bytes(manifest)) > _MAX_SITE_MANIFEST_BYTES:
        raise ReleaseError("installed site manifest exceeds the safety limit")
    return manifest


def _site_manifest_from_verified_tree(
    *,
    release_id: str,
    verified: VerifiedWheel,
    site: Path,
    directories: set[str],
) -> dict[str, object]:
    record_name = f"{verified.dist_info}/RECORD"
    file_rows: list[list[object]] = []
    for name, digest, size_text in sorted(verified.record_rows):
        if name == record_name:
            if digest or size_text:
                raise ReleaseError("installed RECORD self-row is not empty")
            content = _read_installed_member(site, name)
            file_rows.append([name, sha256_bytes(content), len(content)])
            continue
        if not size_text.isdecimal() or str(int(size_text)) != size_text:
            raise ReleaseError(f"installed RECORD size is invalid: {name}")
        file_rows.append([name, _record_digest_hex(digest, name=name), int(size_text)])
    return _validate_site_manifest(
        {
            "schema": _SITE_MANIFEST_SCHEMA,
            "release_id": release_id,
            "wheel_sha256": verified.sha256,
            "directories": sorted(directories),
            "files": file_rows,
        },
        release_id=release_id,
        wheel_sha256=verified.sha256,
        installed_members=len(verified.members),
        installed_directories=len(directories),
    )


def _verify_installed_tree_details(
    release_dir: str | Path,
) -> tuple[dict[str, object], dict[str, object]]:
    source = Path(release_dir).expanduser()
    mode = source.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseError("installed release root is not a real directory")
    root = source.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest_stat = manifest_path.lstat()
    manifest_mode = manifest_stat.st_mode
    if stat.S_ISLNK(manifest_mode) or not stat.S_ISREG(manifest_mode):
        raise ReleaseError("installed release manifest is not a regular file")
    if stat.S_IMODE(manifest_mode) & 0o222 or manifest_stat.st_nlink != 1:
        raise ReleaseError("installed release manifest is writable or hard-linked")
    try:
        manifest = validate_release_manifest(read_json(manifest_path, label="release manifest"))
    except BuildIdentityError as exc:
        raise ReleaseError(str(exc)) from exc
    _require_production_build(
        manifest["build_info"],
        label="installed release manifest",
    )
    if manifest_path.read_bytes() != canonical_json_bytes(manifest):
        raise ReleaseError("installed release manifest is not canonical")
    build_info = manifest.get("build_info")
    if not isinstance(build_info, Mapping):
        raise ReleaseError("installed release manifest has no build-info mapping")
    frozen_directories = _release_requires_frozen_directories(build_info)
    wheel = _verify_release_envelope(
        root,
        wheel_name=str(manifest["wheel"]),
    )
    installed_directories = _verify_release_directories(
        root,
        frozen=frozen_directories,
    )
    wheel_stat = wheel.lstat()
    if (
        stat.S_ISLNK(wheel_stat.st_mode)
        or not stat.S_ISREG(wheel_stat.st_mode)
        or stat.S_IMODE(wheel_stat.st_mode) & 0o222
        or wheel_stat.st_nlink != 1
    ):
        raise ReleaseError("installed wheel artifact is writable, linked, or unsafe")
    verified = verify_wheel(wheel, manifest_path=manifest_path)
    site = root / "site"
    actual_files, site_directories = _installed_site_entries(site)
    expected_files = set(verified.members)
    if actual_files != expected_files:
        raise ReleaseError(
            "installed release file set differs from wheel RECORD; "
            f"missing={sorted(expected_files - actual_files)}, "
            f"extra={sorted(actual_files - expected_files)}"
        )
    for name, digest, size_text in verified.record_rows:
        content = _read_installed_member(site, name)
        if name == f"{verified.dist_info}/RECORD":
            if digest or size_text:
                raise ReleaseError("installed RECORD self-row is not empty")
            continue
        if _record_hash(content) != digest or str(len(content)) != size_text:
            raise ReleaseError(f"installed wheel member failed RECORD verification: {name}")
    site_manifest = _site_manifest_from_verified_tree(
        release_id=str(manifest["release_id"]),
        verified=verified,
        site=site,
        directories=site_directories,
    )
    result = {
        "ok": True,
        "release_id": manifest["release_id"],
        "wheel_sha256": verified.sha256,
        "installed_members": len(verified.members),
        "installed_directories": installed_directories,
        "frozen_directories": frozen_directories,
        "site_manifest_sha256": sha256_bytes(canonical_json_bytes(site_manifest)),
    }
    return result, site_manifest


def verify_installed_tree(release_dir: str | Path) -> dict[str, object]:
    result, _site_manifest = _verify_installed_tree_details(release_dir)
    return result


def _bootstrap_source(layout: ReleaseLayout, python: Path) -> bytes:
    """Return a self-contained launcher that verifies bytes before importing code."""

    root = str(layout.root.resolve())
    state_root = _deployment_state_root(layout)
    state_assignment = (
        f'os.environ["TERMDECK_STATE_ROOT"] = {str(state_root)!r}\n'
        if state_root is not None
        else ""
    )
    template = r'''#!/usr/bin/env __PYTHON_NAME__
from __future__ import annotations
import atexit
import base64
import csv
import ctypes
import errno
import hashlib
import io
import importlib.abc
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__ROOT_REPR__)
STATE = ROOT / "state.json"
RELEASE_VERIFICATION_SCHEMA = "t1000.release-verification.v2"
SITE_MANIFEST_SCHEMA = "t1000.installed-site-manifest.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SITE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RELEASE_VERIFICATION_BYTES = MAX_SITE_MANIFEST_BYTES + 64 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_BYTES = 512 * 1024 * 1024
MAX_WHEEL_ENTRIES = 20_000
MAX_WHEEL_PATH_COMPONENTS = 256
MAX_SITE_DIRECTORIES = 100_000
_RENAME_NOREPLACE = 1
_PUBLICATION_EINTR_RETRIES = 8
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def digest_file(path, *, maximum_bytes):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        entry = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"cannot hash a non-regular file: {path}")
    if entry.st_size < 0 or entry.st_size > maximum_bytes:
        os.close(descriptor)
        raise RuntimeError(f"file exceeds the {maximum_bytes}-byte digest limit: {path}")
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        remaining = entry.st_size
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError(f"file changed while being hashed: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise RuntimeError(f"file grew while being hashed: {path}")
    after = os.stat(path, follow_symlinks=False)
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        raise RuntimeError(f"file changed while being hashed: {path}")
    return digest.hexdigest()


def regular_file(path, label, *, immutable=False, executable=False):
    entry = path.lstat()
    mode = entry.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")
    permissions = stat.S_IMODE(mode)
    if immutable and (permissions & 0o222 or entry.st_nlink != 1):
        raise RuntimeError(f"{label} is writable or hard-linked: {path}")
    if executable and (permissions & 0o100 == 0 or permissions & 0o022):
        raise RuntimeError(f"{label} has unsafe executable permissions: {path}")
    return path


def bounded_bytes(path, label, maximum_bytes):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        entry = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    mode = entry.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        os.close(descriptor)
        raise RuntimeError(f"{label} is not a regular file: {path}")
    if entry.st_size < 0 or entry.st_size > maximum_bytes:
        os.close(descriptor)
        raise RuntimeError(f"{label} exceeds the {maximum_bytes}-byte safety limit")
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        value = handle.read(entry.st_size)
        if len(value) != entry.st_size or handle.read(1):
            raise RuntimeError(f"{label} changed while being read")
    after = path.lstat()
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        raise RuntimeError(f"{label} changed while being read")
    return value


def real_directory(path, label):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"{label} is not a real directory: {path}")
    return path.resolve(strict=True)


def exact_directory_entries(path, label, expected):
    names = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) > len(expected):
                    raise RuntimeError(
                        f"{label} contains unexpected entries outside the immutable release envelope"
                    )
    except OSError as exc:
        raise RuntimeError(f"could not enumerate {label}: {exc}") from exc
    if names != set(expected):
        raise RuntimeError(f"{label} entries differ from the immutable release envelope")


def frozen_directories_required(build_info):
    version = build_info.get("version") if isinstance(build_info, dict) else None
    if not isinstance(version, str):
        return True
    match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[A-Za-z0-9.+-].*)?",
        version,
    )
    if match is None:
        return True
    try:
        numeric = tuple(int(component) for component in match.groups())
    except ValueError:
        return True
    return numeric >= (0, 2, 13)


def safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe wheel member name: {name!r}")
    trailing = name.endswith("/")
    normalized = name[:-1] if trailing else name
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or len(parts) > MAX_WHEEL_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RuntimeError(f"unsafe wheel member path: {name!r}")
    if "/".join(parts) != normalized:
        raise RuntimeError(f"non-canonical wheel member path: {name!r}")
    return normalized


def record_digest_hex(value, name):
    match = re.fullmatch(r"sha256=([A-Za-z0-9_-]{43})", value)
    if match is None:
        raise RuntimeError(f"wheel RECORD SHA-256 is invalid: {name}")
    decoded = base64.urlsafe_b64decode(match.group(1) + "=")
    if len(decoded) != hashlib.sha256().digest_size:
        raise RuntimeError(f"wheel RECORD SHA-256 is invalid: {name}")
    return decoded.hex()


def validate_site_manifest(value, *, release_id, wheel_sha256):
    if not isinstance(value, dict):
        raise RuntimeError("installed site manifest is not an object")
    expected_fields = {"schema", "release_id", "wheel_sha256", "directories", "files"}
    if set(value) != expected_fields:
        raise RuntimeError("installed site manifest fields differ from schema")
    if value["schema"] != SITE_MANIFEST_SCHEMA:
        raise RuntimeError("installed site manifest schema is unsupported")
    if value["release_id"] != release_id:
        raise RuntimeError("installed site manifest release id mismatch")
    if value["wheel_sha256"] != wheel_sha256:
        raise RuntimeError("installed site manifest wheel mismatch")
    directories = value["directories"]
    if not isinstance(directories, list) or not directories or len(directories) > MAX_SITE_DIRECTORIES:
        raise RuntimeError("installed site manifest directory count is unsafe")
    if any(not isinstance(item, str) for item in directories):
        raise RuntimeError("installed site manifest directory path is invalid")
    if directories != sorted(set(directories)) or "." not in directories:
        raise RuntimeError("installed site manifest directories are not canonical")
    directory_set = set(directories)
    for name in directories:
        if name == ".":
            continue
        safe_member(name)
        if PurePosixPath(name).parent.as_posix() not in directory_set:
            raise RuntimeError("installed site manifest directory parent is missing")
    files = value["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_WHEEL_ENTRIES:
        raise RuntimeError("installed site manifest file count is unsafe")
    names = []
    total_size = 0
    for row in files:
        if not isinstance(row, list) or len(row) != 3:
            raise RuntimeError("installed site manifest file row is invalid")
        name, digest, size = row
        if not isinstance(name, str):
            raise RuntimeError("installed site manifest file path is invalid")
        safe_member(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError(f"installed site manifest digest is invalid: {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"installed site manifest size is invalid: {name}")
        if PurePosixPath(name).parent.as_posix() not in directory_set:
            raise RuntimeError(f"installed site manifest file parent is missing: {name}")
        total_size += size
        if total_size > MAX_WHEEL_BYTES:
            raise RuntimeError("installed site manifest total size is unsafe")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise RuntimeError("installed site manifest file paths are not canonical")
    if len(canonical(value)) > MAX_SITE_MANIFEST_BYTES:
        raise RuntimeError("installed site manifest exceeds the safety limit")
    return value


def site_manifest_from_wheel(path, *, release_id, expected_sha256, expected_size):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o222:
            raise RuntimeError("managed wheel is writable, linked, or unsafe")
        if before.st_size != expected_size or before.st_size > MAX_WHEEL_BYTES:
            raise RuntimeError("managed wheel artifact failed size verification")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("managed wheel changed while being hashed")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1) or digest.hexdigest() != expected_sha256:
                raise RuntimeError("managed wheel artifact failed size/SHA-256 verification")
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > MAX_WHEEL_ENTRIES:
                    raise RuntimeError("managed wheel entry count is unsafe")
                names = set()
                files = set()
                file_infos = {}
                directories = {"."}
                total_size = 0
                for info in infos:
                    name = safe_member(info.filename)
                    if name in names:
                        raise RuntimeError(f"managed wheel contains duplicate member: {name}")
                    names.add(name)
                    if info.flag_bits & 0x1 or info.file_size < 0 or info.file_size > 128 * 1024 * 1024:
                        raise RuntimeError(f"managed wheel member is unsafe: {name}")
                    total_size += info.file_size
                    if total_size > MAX_WHEEL_BYTES:
                        raise RuntimeError("managed wheel uncompressed size exceeds safety limit")
                    if info.is_dir():
                        directories.add(name)
                    else:
                        files.add(name)
                        file_infos[name] = info
                    parent = PurePosixPath(name).parent
                    while parent.as_posix() != ".":
                        directories.add(parent.as_posix())
                        if len(directories) > MAX_SITE_DIRECTORIES:
                            raise RuntimeError("managed wheel directory count is unsafe")
                        parent = parent.parent
                records = sorted(name for name in files if name.endswith(".dist-info/RECORD"))
                if len(records) != 1:
                    raise RuntimeError("managed wheel must contain exactly one RECORD")
                record_name = records[0]
                if file_infos[record_name].file_size > MAX_SITE_MANIFEST_BYTES:
                    raise RuntimeError("managed wheel RECORD exceeds the safety limit")
                record_bytes = archive.read(record_name)
                if len(record_bytes) > MAX_SITE_MANIFEST_BYTES:
                    raise RuntimeError("managed wheel RECORD exceeds the safety limit")
                pinned_members = {}
                for name in sorted(files):
                    info = file_infos[name]
                    content = archive.read(info)
                    if len(content) != info.file_size:
                        raise RuntimeError(f"managed wheel member changed size: {name}")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    pinned_members[name] = (content, bool(mode & 0o111))
        try:
            record_text = record_bytes.decode("utf-8")
            rows = list(csv.reader(io.StringIO(record_text)))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise RuntimeError(f"managed wheel RECORD is invalid: {exc}") from exc
        record_map = {}
        for row in rows:
            if len(row) != 3:
                raise RuntimeError("managed wheel RECORD row is malformed")
            name, digest_value, size_text = row
            name = safe_member(name)
            if name in record_map:
                raise RuntimeError(f"managed wheel RECORD path is duplicated: {name}")
            record_map[name] = (digest_value, size_text)
        if set(record_map) != files:
            raise RuntimeError("managed wheel RECORD coverage differs from archive")
        file_rows = []
        for name in sorted(files):
            digest_value, size_text = record_map[name]
            if name == record_name:
                if digest_value or size_text:
                    raise RuntimeError("managed wheel RECORD self-row is not empty")
                file_rows.append([name, digest_bytes(record_bytes), len(record_bytes)])
                continue
            if not size_text.isdecimal() or str(int(size_text)) != size_text:
                raise RuntimeError(f"managed wheel RECORD size is invalid: {name}")
            file_rows.append([name, record_digest_hex(digest_value, name), int(size_text)])
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        identity = lambda entry: (
            entry.st_dev,
            entry.st_ino,
            entry.st_size,
            entry.st_mtime_ns,
            entry.st_ctime_ns,
        )
        if identity(after) != identity(before) or identity(path_after) != identity(before):
            raise RuntimeError("managed wheel changed while being verified")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"managed wheel is invalid: {exc}") from exc
    finally:
        os.close(descriptor)
    return (
        validate_site_manifest(
            {
                "schema": SITE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "wheel_sha256": expected_sha256,
                "directories": sorted(directories),
                "files": file_rows,
            },
            release_id=release_id,
            wheel_sha256=expected_sha256,
        ),
        pinned_members,
    )


def verify_installed_site(site, site_manifest, *, frozen):
    site_root = site.resolve(strict=True)
    directories = set(site_manifest["directories"])
    children = {name: {} for name in directories}
    for name in directories:
        if name == ".":
            continue
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"installed site path collision: {name}")
        children[parent][child] = "directory"
    for name, _digest, _size in site_manifest["files"]:
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"installed site path collision: {name}")
        children[parent][child] = "file"
    for name in sorted(directories, key=lambda value: (value.count("/"), value)):
        target = site if name == "." else site.joinpath(*PurePosixPath(name).parts)
        entry = target.lstat()
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise RuntimeError(f"installed site directory is unsafe: {name}")
        target.resolve(strict=True).relative_to(site_root)
        if frozen and stat.S_IMODE(entry.st_mode) != 0o555:
            raise RuntimeError(f"installed site directory is not frozen: {name}")
        expected = children[name]
        observed = {}
        with os.scandir(target) as entries:
            for index, child in enumerate(entries):
                if index >= len(expected):
                    raise RuntimeError(f"installed site directory contains extra entries: {name}")
                if child.is_symlink():
                    raise RuntimeError(f"installed site contains a symlink: {child.path}")
                if child.is_dir(follow_symlinks=False):
                    observed[child.name] = "directory"
                elif child.is_file(follow_symlinks=False):
                    observed[child.name] = "file"
                else:
                    raise RuntimeError(f"installed site contains a special file: {child.path}")
        if observed != expected:
            raise RuntimeError(f"installed site directory entries mismatch: {name}")
    for name, expected_digest, expected_size in site_manifest["files"]:
        target = site.joinpath(*PurePosixPath(name).parts)
        entry = regular_file(target, "installed site member", immutable=True).lstat()
        target.resolve(strict=True).relative_to(site_root)
        if entry.st_size != expected_size:
            raise RuntimeError(f"installed site member size mismatch: {name}")
        if digest_file(target, maximum_bytes=expected_size) != expected_digest:
            raise RuntimeError(f"installed site member SHA-256 mismatch: {name}")


def snapshot_identity(entry):
    return (
        entry.st_dev,
        entry.st_ino,
        stat.S_IFMT(entry.st_mode),
        stat.S_IMODE(entry.st_mode),
        entry.st_nlink,
        entry.st_size,
    )


def _directory_open_flags():
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if (
        isinstance(directory, bool)
        or not isinstance(directory, int)
        or directory == 0
        or isinstance(nofollow, bool)
        or not isinstance(nofollow, int)
        or nofollow == 0
    ):
        raise RuntimeError(
            "secure descriptor-relative import cleanup requires O_DIRECTORY and O_NOFOLLOW"
        )
    return os.O_RDONLY | directory | getattr(os, "O_CLOEXEC", 0) | nofollow


def _fsync_descriptor(descriptor, *, label):
    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise RuntimeError(f"could not fsync {label}: {exc}") from exc
        if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
            break
    raise RuntimeError(
        f"could not fsync {label}: interrupted {_PUBLICATION_EINTR_RETRIES} times"
    )


def _renameat2(descriptor, source, destination, *, flags, label):
    if flags != _RENAME_NOREPLACE:
        raise RuntimeError(f"unsupported secure rename flags for {label}")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise RuntimeError(
            "secure descriptor-relative import cleanup requires renameat2(RENAME_NOREPLACE)"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        result = renameat2(
            descriptor,
            os.fsencode(source),
            descriptor,
            os.fsencode(destination),
            flags,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EINTR:
            if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
                break
            continue
        if error == errno.ENOENT:
            raise FileNotFoundError(error, os.strerror(error), source)
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        raise OSError(error, os.strerror(error), source, destination)
    raise RuntimeError(
        f"secure descriptor-relative rename interrupted {_PUBLICATION_EINTR_RETRIES} times"
    )


def _unlinkat(descriptor, name, *, label):
    for attempt in range(_PUBLICATION_EINTR_RETRIES):
        try:
            os.unlink(name, dir_fd=descriptor)
            return
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise
        if attempt + 1 >= _PUBLICATION_EINTR_RETRIES:
            break
    raise RuntimeError(
        f"could not unlink {label}: interrupted {_PUBLICATION_EINTR_RETRIES} times"
    )


def _snapshot_components(name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError(f"unsafe import snapshot path: {name}")
    return path.parts


def _open_snapshot_parent(descriptor, name):
    components = _snapshot_components(name)
    if not components:
        raise RuntimeError(f"import snapshot path has no leaf: {name}")
    current = os.dup(descriptor)
    try:
        for component in components[:-1]:
            child = os.open(component, _directory_open_flags(), dir_fd=current)
            os.close(current)
            current = child
        return current, components[-1]
    except Exception:
        os.close(current)
        raise


def _remove_snapshot_file(descriptor, name, expected=None):
    """Remove one materialized leaf through its retained root descriptor."""

    parent, leaf = _open_snapshot_parent(descriptor, name)
    file_descriptor = -1
    try:
        try:
            observed = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise RuntimeError(f"import snapshot member is unsafe during cleanup: {name}")
        observed_identity = snapshot_identity(observed)
        if expected is not None and observed_identity != expected:
            raise RuntimeError(f"import snapshot member changed during cleanup: {name}")
        file_descriptor = os.open(
            leaf,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        if snapshot_identity(os.fstat(file_descriptor)) != observed_identity:
            raise RuntimeError(f"import snapshot member changed during cleanup: {name}")
        for _attempt in range(8):
            disposal = f".termdeck-snapshot-dispose-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                _renameat2(
                    parent,
                    leaf,
                    disposal,
                    flags=_RENAME_NOREPLACE,
                    label="import snapshot member cleanup",
                )
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(f"could not allocate import snapshot disposal: {name}")
        if snapshot_identity(os.stat(disposal, dir_fd=parent, follow_symlinks=False)) != observed_identity:
            raise RuntimeError(f"import snapshot member changed during cleanup: {name}")
        _unlinkat(parent, disposal, label="import snapshot member cleanup")
        _fsync_descriptor(parent, label="import snapshot member cleanup parent")
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(parent)


def _remove_snapshot_directory(descriptor, name, expected=None):
    parent, leaf = _open_snapshot_parent(descriptor, name)
    try:
        try:
            observed = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise RuntimeError(f"import snapshot directory is unsafe during cleanup: {name}")
        observed_identity = snapshot_identity(observed)
        if expected is not None and observed_identity != expected:
            raise RuntimeError(f"import snapshot directory changed during cleanup: {name}")
        for _attempt in range(8):
            disposal = f".termdeck-snapshot-dir-dispose-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                _renameat2(
                    parent,
                    leaf,
                    disposal,
                    flags=_RENAME_NOREPLACE,
                    label="import snapshot directory cleanup",
                )
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(f"could not allocate import snapshot directory disposal: {name}")
        if snapshot_identity(os.stat(disposal, dir_fd=parent, follow_symlinks=False)) != observed_identity:
            raise RuntimeError(f"import snapshot directory changed during cleanup: {name}")
        os.rmdir(disposal, dir_fd=parent)
        _fsync_descriptor(parent, label="import snapshot directory cleanup parent")
    finally:
        os.close(parent)


def discard_partial_snapshot(snapshot, descriptor, files, directories, identities=None):
    try:
        for name in reversed(files):
            try:
                _remove_snapshot_file(
                    descriptor,
                    name,
                    None if identities is None else identities.get(name),
                )
            except (OSError, RuntimeError):
                pass
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            try:
                _remove_snapshot_directory(
                    descriptor,
                    name,
                    None if identities is None else identities.get(name),
                )
            except (OSError, RuntimeError):
                pass
        try:
            named = snapshot.lstat()
            opened = os.fstat(descriptor)
            if snapshot_identity(named) == snapshot_identity(opened):
                snapshot.rmdir()
        except (OSError, RuntimeError):
            pass
    finally:
        os.close(descriptor)


def cleanup_import_snapshot(snapshot, descriptor, files, directories, identities):
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    try:
        named_root = snapshot.lstat()
        opened_root = os.fstat(descriptor)
        if snapshot_identity(named_root) != identities["."]:
            return
        if snapshot_identity(opened_root) != identities["."]:
            return
        for name, expected in identities.items():
            if name == ".":
                continue
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            if snapshot_identity(target.lstat()) != expected:
                return
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
            expected = identities[name]
            target.chmod(0o700)
            observed = (
                snapshot_identity(os.fstat(descriptor))
                if name == "."
                else snapshot_identity(target.lstat())
            )
            expected_after_mode = expected[:3] + (0o700,) + expected[4:]
            if observed != expected_after_mode:
                return
        for name in files:
            _remove_snapshot_file(descriptor, name, identities[name])
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            expected = identities[name]
            observed = snapshot_identity(target.lstat())
            if observed[:3] != expected[:3] or observed[3] != 0o700:
                return
            _remove_snapshot_directory(descriptor, name, observed)
        expected_root = identities["."]
        named_root = snapshot_identity(snapshot.lstat())
        opened_root = snapshot_identity(os.fstat(descriptor))
        if (
            named_root[:3] != expected_root[:3]
            or named_root[3] != 0o700
            or opened_root[:3] != expected_root[:3]
            or opened_root[3] != 0o700
        ):
            return
        snapshot.rmdir()
    except (OSError, RuntimeError):
        pass
    finally:
        os.close(descriptor)


def verify_import_snapshot(
    snapshot,
    descriptor,
    files,
    directories,
    identities,
    *,
    expected_device,
):
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    opened_root = os.fstat(descriptor)
    named_root = snapshot.lstat()
    if snapshot_identity(opened_root) != identities["."]:
        raise RuntimeError("verified import snapshot root descriptor changed")
    if snapshot_identity(named_root) != identities["."]:
        raise RuntimeError("verified import snapshot root path changed")
    if opened_root.st_dev != expected_device:
        raise RuntimeError("verified import snapshot is not on the managed release device")
    if descriptor_root.resolve(strict=True) != snapshot.resolve(strict=True):
        raise RuntimeError("verified import snapshot descriptor path mismatch")

    children = {name: {} for name in directories}
    for name in directories:
        if name == ".":
            continue
        parent = PurePosixPath(name).parent.as_posix()
        children[parent][PurePosixPath(name).name] = "directory"
    for name in files:
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"verified import snapshot path collision: {name}")
        children[parent][child] = "file"

    for name in sorted(directories, key=lambda value: (value.count("/"), value)):
        target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
        entry = os.fstat(descriptor) if name == "." else target.lstat()
        if snapshot_identity(entry) != identities[name]:
            raise RuntimeError(f"verified import snapshot directory changed: {name}")
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise RuntimeError(f"verified import snapshot directory is unsafe: {name}")
        if stat.S_IMODE(entry.st_mode) != 0o500:
            raise RuntimeError(f"verified import snapshot directory mode is unsafe: {name}")
        observed = {}
        with os.scandir(target) as entries:
            for index, child in enumerate(entries):
                if index >= len(children[name]):
                    raise RuntimeError(f"verified import snapshot directory has extra entries: {name}")
                if child.is_symlink():
                    raise RuntimeError(f"verified import snapshot contains a symlink: {child.path}")
                if child.is_dir(follow_symlinks=False):
                    observed[child.name] = "directory"
                elif child.is_file(follow_symlinks=False):
                    observed[child.name] = "file"
                else:
                    raise RuntimeError(f"verified import snapshot contains a special file: {child.path}")
        if observed != children[name]:
            raise RuntimeError(f"verified import snapshot directory entries mismatch: {name}")

    for name, (content, mode, digest) in files.items():
        target = descriptor_root.joinpath(*PurePosixPath(name).parts)
        entry = target.lstat()
        if snapshot_identity(entry) != identities[name]:
            raise RuntimeError(f"verified import snapshot member changed: {name}")
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise RuntimeError(f"verified import snapshot member is unsafe: {name}")
        if stat.S_IMODE(entry.st_mode) != mode or entry.st_nlink != 1:
            raise RuntimeError(f"verified import snapshot member mode is unsafe: {name}")
        if digest_file(target, maximum_bytes=len(content)) != digest:
            raise RuntimeError(f"verified import snapshot member digest mismatch: {name}")
    return descriptor_root


def materialize_import_snapshot(root, release, site_manifest, pinned_members):
    manifest_rows = {
        name: (digest, size)
        for name, digest, size in site_manifest["files"]
    }
    if set(pinned_members) != set(manifest_rows):
        raise RuntimeError("verified import bytes differ from the installed site descriptor")
    files = {}
    for name in sorted(pinned_members):
        content, executable = pinned_members[name]
        expected_digest, expected_size = manifest_rows[name]
        if len(content) != expected_size or digest_bytes(content) != expected_digest:
            raise RuntimeError(f"verified import bytes do not match the descriptor: {name}")
        files[name] = (
            content,
            0o500 if executable else 0o400,
            expected_digest,
        )
    directories = tuple(site_manifest["directories"])
    snapshot = Path(
        tempfile.mkdtemp(
            prefix=f".bootstrap-import-{os.getpid()}-",
            dir=root,
        )
    )
    try:
        descriptor = os.open(
            snapshot,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception:
        try:
            snapshot.rmdir()
        except OSError:
            pass
        raise
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    created_directories = ["."]
    created_files = []
    created_file_identities = {}
    try:
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
        ):
            descriptor_root.joinpath(*PurePosixPath(name).parts).mkdir(mode=0o700)
            created_directories.append(name)
        for name, (content, mode, _digest) in files.items():
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            member_descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(member_descriptor, content[offset:])
                    if written <= 0:
                        raise RuntimeError(f"could not materialize verified import member: {name}")
                    offset += written
                os.fchmod(member_descriptor, mode)
            finally:
                os.close(member_descriptor)
            created_files.append(name)
            created_file_identities[name] = snapshot_identity(target.lstat())
        for name in sorted(
            created_directories,
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
            target.chmod(0o500)
        identities = {".": snapshot_identity(os.fstat(descriptor))}
        for name in created_directories:
            if name == ".":
                continue
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            identities[name] = snapshot_identity(target.lstat())
        for name in created_files:
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            identities[name] = snapshot_identity(target.lstat())
        import_root = verify_import_snapshot(
            snapshot,
            descriptor,
            files,
            directories,
            identities,
            expected_device=release.lstat().st_dev,
        )
    except Exception:
        discard_partial_snapshot(
            snapshot,
            descriptor,
            created_files,
            created_directories,
            created_file_identities,
        )
        raise
    return snapshot, descriptor, import_root, tuple(files), directories, identities


class SnapshotResourceReader:
    def __init__(self, root):
        self.root = root

    def _target(self, resource):
        if not isinstance(resource, str) or not resource or Path(resource).name != resource:
            raise FileNotFoundError(resource)
        return self.root / resource

    def open_resource(self, resource):
        return self._target(resource).open("rb")

    def resource_path(self, resource):
        return str(self._target(resource))

    def is_resource(self, resource):
        try:
            entry = self._target(resource).lstat()
        except OSError:
            return False
        return not stat.S_ISLNK(entry.st_mode) and stat.S_ISREG(entry.st_mode)

    def contents(self):
        with os.scandir(self.root) as entries:
            return tuple(entry.name for entry in entries)

    def files(self):
        return self.root


class VerifiedSnapshotImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root, pinned_members):
        self.root = root
        self.modules = {}
        for name, (content, _executable) in pinned_members.items():
            if not name.startswith("termdeck/") or not name.endswith(".py"):
                continue
            parts = list(PurePosixPath(name).parts)
            is_package = parts[-1] == "__init__.py"
            module_parts = parts[:-1] if is_package else parts[:-1] + [parts[-1][:-3]]
            if not module_parts or any(not part.isidentifier() for part in module_parts):
                continue
            fullname = ".".join(module_parts)
            if fullname in self.modules:
                raise RuntimeError(f"verified import module path collision: {fullname}")
            self.modules[fullname] = (name, content, is_package)
        package = self.modules.get("termdeck")
        if package is None or not package[2]:
            raise RuntimeError("verified import bytes have no termdeck package")

    def find_spec(self, fullname, path=None, target=None):
        record = self.modules.get(fullname)
        if record is None:
            if fullname == "termdeck" or fullname.startswith("termdeck."):
                raise ModuleNotFoundError(f"module is absent from verified import bytes: {fullname}")
            return None
        name, _content, is_package = record
        spec = importlib.util.spec_from_loader(
            fullname,
            self,
            is_package=is_package,
        )
        if spec is None:
            raise ImportError(f"could not create verified import spec: {fullname}")
        spec.cached = None
        if is_package:
            spec.submodule_search_locations = [
                str(self.root.joinpath(*PurePosixPath(name).parent.parts))
            ]
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name, content, _is_package = self.modules[module.__name__]
        filename = str(self.root.joinpath(*PurePosixPath(name).parts))
        code = compile(content, filename, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_filename(self, fullname):
        name, _content, _is_package = self.modules[fullname]
        return str(self.root.joinpath(*PurePosixPath(name).parts))

    def get_resource_reader(self, fullname):
        name, _content, is_package = self.modules.get(fullname, (None, None, False))
        if not is_package or not isinstance(name, str):
            return None
        return SnapshotResourceReader(
            self.root.joinpath(*PurePosixPath(name).parent.parts)
        )


def validate_release_verification(receipt, *, release_id, manifest, manifest_bytes):
    if not isinstance(receipt, dict):
        raise RuntimeError("release verification receipt is not an object")
    expected_fields = {
        "schema",
        "release_id",
        "manifest_sha256",
        "wheel_sha256",
        "wheel_size",
        "installed_members",
        "installed_directories",
        "site_directories",
        "frozen_directories",
        "site_manifest",
        "site_manifest_sha256",
        "method",
    }
    if set(receipt) != expected_fields:
        raise RuntimeError("release verification receipt fields differ from schema")
    if receipt["schema"] != RELEASE_VERIFICATION_SCHEMA:
        raise RuntimeError("release verification receipt schema is unsupported")
    if receipt["release_id"] != release_id:
        raise RuntimeError("release verification receipt release id mismatch")
    if receipt["manifest_sha256"] != digest_bytes(manifest_bytes):
        raise RuntimeError("release verification receipt manifest mismatch")
    if receipt["wheel_sha256"] != manifest.get("wheel_sha256"):
        raise RuntimeError("release verification receipt wheel mismatch")
    if receipt["wheel_size"] != manifest.get("wheel_size"):
        raise RuntimeError("release verification receipt wheel size mismatch")
    for field in ("manifest_sha256", "wheel_sha256"):
        if not isinstance(receipt[field], str) or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None:
            raise RuntimeError(f"release verification receipt {field} is invalid")
    for field in ("wheel_size", "installed_members", "installed_directories", "site_directories"):
        value = receipt[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"release verification receipt {field} is invalid")
    if receipt["installed_directories"] != receipt["site_directories"] + 2:
        raise RuntimeError("release verification receipt directory count mismatch")
    if not isinstance(receipt["frozen_directories"], bool):
        raise RuntimeError("release verification receipt frozen policy is invalid")
    if receipt["frozen_directories"] != frozen_directories_required(manifest.get("build_info")):
        raise RuntimeError("release verification receipt frozen policy mismatch")
    site_manifest = validate_site_manifest(
        receipt["site_manifest"],
        release_id=release_id,
        wheel_sha256=receipt["wheel_sha256"],
    )
    if len(site_manifest["files"]) != receipt["installed_members"]:
        raise RuntimeError("release verification receipt site member count mismatch")
    if len(site_manifest["directories"]) != receipt["site_directories"]:
        raise RuntimeError("release verification receipt site directory count mismatch")
    site_manifest_sha256 = receipt["site_manifest_sha256"]
    if not isinstance(site_manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", site_manifest_sha256) is None:
        raise RuntimeError("release verification receipt site manifest digest is invalid")
    if digest_bytes(canonical(site_manifest)) != site_manifest_sha256:
        raise RuntimeError("release verification receipt site manifest digest mismatch")
    if receipt["method"] != "full-records-site-manifest-v2":
        raise RuntimeError("release verification receipt method is unsupported")
    return site_manifest


try:
    regular_file(STATE, "managed state")
    raw_state = bounded_bytes(STATE, "managed state", MAX_MANIFEST_BYTES)
    state = json.loads(raw_state.decode("utf-8"))
    if canonical(state) != raw_state:
        raise RuntimeError("managed state is not canonical")
    state_fields = {
        "schema",
        "generation",
        "current_release_id",
        "previous_release_id",
        "manifest_sha256",
        "python_executable",
        "bootstrap_sha256",
        "launcher_path",
        "launcher_sha256",
        "legacy_launcher",
    }
    if set(state) != state_fields or state.get("schema") != "t1000.managed-state.v2":
        raise RuntimeError("managed state schema or fields mismatch")
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise RuntimeError("managed state generation is invalid")
    release_id = state.get("current_release_id")
    if not isinstance(release_id, str) or re.fullmatch(r"[0-9a-f]{64}", release_id) is None:
        raise RuntimeError("managed state has no valid current release")
    previous_id = state.get("previous_release_id")
    if previous_id is not None and (
        not isinstance(previous_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", previous_id) is None
        or previous_id == release_id
    ):
        raise RuntimeError("managed previous release is invalid")
    for field in ("manifest_sha256", "bootstrap_sha256", "launcher_sha256"):
        if not isinstance(state.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", state[field]) is None:
            raise RuntimeError(f"managed {field} is invalid")

    configured_python = Path(state.get("python_executable", ""))
    if not configured_python.is_absolute():
        raise RuntimeError("managed Python path is not absolute")
    regular_file(configured_python, "managed Python", executable=True)
    if configured_python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise RuntimeError("managed Python path does not match the running interpreter")
    bootstrap = regular_file(Path(__file__), "managed bootstrap", executable=True)
    expected_bootstrap = regular_file(
        ROOT / "bootstrap.py", "managed bootstrap", executable=True
    )
    if bootstrap.resolve(strict=True) != expected_bootstrap.resolve(strict=True):
        raise RuntimeError("managed bootstrap path mismatch")
    if digest_file(bootstrap, maximum_bytes=MAX_RUNTIME_BYTES) != state["bootstrap_sha256"]:
        raise RuntimeError("managed bootstrap SHA-256 mismatch")
    launcher = Path(state.get("launcher_path", ""))
    if not launcher.is_absolute():
        raise RuntimeError("managed launcher path is not absolute")
    regular_file(launcher, "managed launcher", executable=True)
    if digest_file(launcher, maximum_bytes=MAX_RUNTIME_BYTES) != state["launcher_sha256"]:
        raise RuntimeError("managed launcher SHA-256 mismatch")

    releases_root = real_directory(ROOT / "releases", "managed releases root")
    release_path = releases_root / release_id
    release = real_directory(release_path, "managed release")
    release.relative_to(releases_root)
    manifest_path = regular_file(
        release / "manifest.json", "managed manifest", immutable=True
    )
    manifest_bytes = bounded_bytes(manifest_path, "managed manifest", MAX_MANIFEST_BYTES)
    if digest_bytes(manifest_bytes) != state.get("manifest_sha256"):
        raise RuntimeError("managed manifest does not match state.json")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if canonical(manifest) != manifest_bytes:
        raise RuntimeError("managed manifest is not canonical")
    if manifest.get("schema") != "t1000.release-manifest.v1":
        raise RuntimeError("managed manifest schema mismatch")
    if manifest.get("release_id") != release_id:
        raise RuntimeError("managed manifest release_id mismatch")

    build_info = manifest.get("build_info")
    if not isinstance(build_info, dict):
        raise RuntimeError("managed manifest has no build info")
    if build_info.get("build_mode") != "release":
        raise RuntimeError("managed build is not a production release")
    build_fields = {
        key: build_info.get(key)
        for key in (
            "schema",
            "version",
            "source_commit",
            "source_tree_sha256",
            "source_date_epoch",
            "build_mode",
        )
    }
    if digest_bytes(canonical(build_fields)) != build_info.get("build_id"):
        raise RuntimeError("managed build_id mismatch")
    wheel_hash = manifest.get("wheel_sha256")
    if not isinstance(wheel_hash, str) or re.fullmatch(r"[0-9a-f]{64}", wheel_hash) is None:
        raise RuntimeError("managed wheel SHA-256 is invalid")
    wheel_size = manifest.get("wheel_size")
    if isinstance(wheel_size, bool) or not isinstance(wheel_size, int) or wheel_size <= 0:
        raise RuntimeError("managed wheel size is invalid")
    expected_release = digest_bytes(
        canonical({"build_id": build_info.get("build_id"), "wheel_sha256": wheel_hash})
    )
    if expected_release != release_id:
        raise RuntimeError("managed release_id does not bind build and wheel")

    wheel_name = manifest.get("wheel")
    if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
        raise RuntimeError("managed wheel name is unsafe")
    exact_directory_entries(
        release,
        "managed release",
        {"site", "artifacts", "manifest.json"},
    )
    artifacts = real_directory(release / "artifacts", "managed release artifacts")
    exact_directory_entries(
        artifacts,
        "managed release artifacts",
        {wheel_name},
    )
    wheel = regular_file(
        artifacts / wheel_name, "managed wheel", immutable=True
    )
    expected_site_manifest, pinned_members = site_manifest_from_wheel(
        wheel,
        release_id=release_id,
        expected_sha256=wheel_hash,
        expected_size=wheel_size,
    )

    site = real_directory(release / "site", "managed release site")
    if frozen_directories_required(build_info):
        for directory, label in (
            (release, "managed release"),
            (artifacts, "managed release artifacts"),
            (site, "managed release site"),
        ):
            if stat.S_IMODE(directory.lstat().st_mode) != 0o555:
                raise RuntimeError(f"{label} is not frozen")
    build_info_path = regular_file(
        site / "termdeck" / "_build_info.json",
        "bundled build info",
        immutable=True,
    )
    build_info_bytes = bounded_bytes(
        build_info_path,
        "bundled build info",
        MAX_MANIFEST_BYTES,
    )
    try:
        bundled_build_info = json.loads(build_info_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"bundled build info is invalid: {exc}") from exc
    if bundled_build_info != build_info:
        raise RuntimeError("bundled build info does not match the manifest")
    verification_path = ROOT / f"verification-{release_id}.json"
    regular_file(
        verification_path,
        "release verification receipt",
        immutable=True,
    )
    verification_bytes = bounded_bytes(
        verification_path,
        "release verification receipt",
        MAX_RELEASE_VERIFICATION_BYTES,
    )
    try:
        verification = json.loads(verification_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"release verification receipt is invalid: {exc}") from exc
    if canonical(verification) != verification_bytes:
        raise RuntimeError("release verification receipt is not canonical")
    site_manifest = validate_release_verification(
        verification,
        release_id=release_id,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
    )
    if site_manifest != expected_site_manifest:
        raise RuntimeError("release verification receipt is not derived from the managed wheel")
    verify_installed_site(
        site,
        site_manifest,
        frozen=frozen_directories_required(build_info),
    )

    latest_state = bounded_bytes(STATE, "managed state", MAX_MANIFEST_BYTES)
    if latest_state != raw_state:
        try:
            retry = int(os.environ.get("T1000_BOOTSTRAP_STATE_RETRY", "0"))
        except ValueError:
            retry = 0
        if retry >= 3:
            raise RuntimeError("managed state changed repeatedly during launch")
        os.environ["T1000_BOOTSTRAP_STATE_RETRY"] = str(retry + 1)
        os.execv(
            str(configured_python),
            [
                str(configured_python),
                "-I",
                "-B",
                str(bootstrap),
                *sys.argv[1:],
            ],
        )
    (
        import_snapshot,
        import_snapshot_descriptor,
        import_root,
        import_snapshot_files,
        import_snapshot_directories,
        import_snapshot_identities,
    ) = materialize_import_snapshot(
        ROOT,
        release,
        site_manifest,
        pinned_members,
    )
    atexit.register(
        cleanup_import_snapshot,
        import_snapshot,
        import_snapshot_descriptor,
        import_snapshot_files,
        import_snapshot_directories,
        import_snapshot_identities,
    )
except Exception as exc:
    print(f"t1000 managed launcher refused an invalid release: {exc}", file=sys.stderr)
    raise SystemExit(78)

os.environ.pop("T1000_BOOTSTRAP_STATE_RETRY", None)
os.environ["T1000_MANAGED_ROOT"] = str(ROOT)
__STATE_ROOT_ASSIGNMENT__os.environ["T1000_MANAGED_BIN_DIR"] = str(launcher.parent)
os.environ["T1000_RELEASE_MANIFEST"] = str(manifest_path)
os.environ["T1000_RELEASE_STATE"] = str(STATE)
verified_snapshot_importer = VerifiedSnapshotImporter(import_root, pinned_members)
sys.meta_path.insert(0, verified_snapshot_importer)
sys.path.insert(0, str(import_root))
del pinned_members
try:
    from termdeck.storage_fabric import require_hot_storage
    require_hot_storage(
        managed_root=ROOT,
        launcher_path=launcher,
        production=True,
    )
except Exception as exc:
    print(f"t1000 managed launcher refused unsafe storage: {exc}", file=sys.stderr)
    raise SystemExit(78)
from termdeck.build_info import release_status
status = release_status(verify_artifact=False, managed_root=ROOT)
if not status.get("ok") or status.get("release_id") != release_id:
    print("t1000 managed launcher rejected inconsistent bundled identity", file=sys.stderr)
    raise SystemExit(78)
from termdeck.cli import main
sys.argv[0] = "t1000"
raise SystemExit(main())
'''
    source = (
        template.replace("__PYTHON_NAME__", python.name)
        .replace("__ROOT_REPR__", repr(root))
        .replace("__STATE_ROOT_ASSIGNMENT__", state_assignment)
    )
    return source.encode("utf-8")


def _launcher_source(layout: ReleaseLayout, python: Path) -> bytes:
    state_root = _deployment_state_root(layout)
    state_export = (
        f"export TERMDECK_STATE_ROOT={shlex.quote(str(state_root))}\n"
        if state_root is not None
        else ""
    )
    command = f'exec {shlex.quote(str(python))} -I -B {shlex.quote(str(layout.bootstrap_path))} "$@"\n'
    return ("#!/bin/sh\nset -eu\n" + state_export + command).encode("utf-8")


def _activation_bundle(
    bootstrap: bytes,
    launcher: bytes,
    *,
    origin: str,
) -> _ActivationBundle:
    """Validate and bind one complete managed activation template."""

    if origin not in {"current-source", "selected-release"}:
        raise ReleaseError(f"unsupported activation-bundle origin: {origin}")
    for label, content in (("bootstrap", bootstrap), ("launcher", launcher)):
        if not isinstance(content, bytes) or not content:
            raise ReleaseError(f"managed {label} source is empty or not bytes")
        if len(content) > _MAX_ACTIVATION_SOURCE_BYTES:
            raise ReleaseError(f"managed {label} source exceeds the byte limit")
        if b"\x00" in content:
            raise ReleaseError(f"managed {label} source contains a NUL byte")
    return _ActivationBundle(
        bootstrap=bootstrap,
        launcher=launcher,
        bootstrap_sha256=sha256_bytes(bootstrap),
        launcher_sha256=sha256_bytes(launcher),
        origin=origin,
    )


def _validated_activation_bundle(
    bundle: _ActivationBundle,
    *,
    expected_origin: str | None = None,
) -> _ActivationBundle:
    if not isinstance(bundle, _ActivationBundle):
        raise ReleaseError("managed activation bundle has an invalid type")
    validated = _activation_bundle(
        bundle.bootstrap,
        bundle.launcher,
        origin=bundle.origin,
    )
    if expected_origin is not None and validated.origin != expected_origin:
        raise ReleaseError(
            "managed activation bundle has the wrong origin: "
            f"{validated.origin!r} != {expected_origin!r}"
        )
    if (
        validated.bootstrap_sha256 != bundle.bootstrap_sha256
        or validated.launcher_sha256 != bundle.launcher_sha256
    ):
        raise ReleaseError("managed activation bundle hashes do not match its bytes")
    return validated


def _current_activation_bundle(
    layout: ReleaseLayout,
    interpreter: Path,
) -> _ActivationBundle:
    return _activation_bundle(
        _bootstrap_source(layout, interpreter),
        _launcher_source(layout, interpreter),
        origin="current-source",
    )


def _state_activation_bundle(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    interpreter: Path,
    *,
    expected_activation: _ActivationBundle | None = None,
    verify_selected_release: bool,
) -> _ActivationBundle:
    """Authenticate state hashes against code, never against state itself.

    Current-source hashes are accepted directly. Historical hashes must match the
    exact activation deterministically derived from the selected immutable wheel.
    Install uses the wheel-only path so it can repair a contaminated extracted
    tree; status verification additionally requires the selected release tree.
    """

    current = state.get("current_release_id")
    if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
        raise ReleaseError("managed current release id is invalid")
    current_activation = _current_activation_bundle(layout, interpreter)
    state_bootstrap_hash = state.get("bootstrap_sha256")
    state_launcher_hash = state.get("launcher_sha256")
    if (
        state_bootstrap_hash == current_activation.bootstrap_sha256
        and state_launcher_hash == current_activation.launcher_sha256
    ):
        return current_activation

    if expected_activation is not None:
        selected = _validated_activation_bundle(
            expected_activation,
            expected_origin="selected-release",
        )
    elif verify_selected_release:
        selected = _verified_release_activation_bundle(layout, current, interpreter)
    else:
        selected = _derive_release_activation_bundle(layout, current, interpreter)
    if state_bootstrap_hash != selected.bootstrap_sha256:
        raise ReleaseError("managed state names a non-canonical bootstrap identity")
    if state_launcher_hash != selected.launcher_sha256:
        raise ReleaseError("managed state names a non-canonical launcher identity")
    return selected


def _decode_activation_probe_source(
    payload: Mapping[str, object],
    *,
    label: str,
) -> bytes:
    encoded = payload.get(f"{label}_base64")
    size = payload.get(f"{label}_size")
    digest = payload.get(f"{label}_sha256")
    if not isinstance(encoded, str) or not encoded:
        raise ReleaseError(f"activation probe returned no {label} source")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ReleaseError(f"activation probe returned an invalid {label} size")
    if size > _MAX_ACTIVATION_SOURCE_BYTES:
        raise ReleaseError(f"activation probe {label} source exceeds the byte limit")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ReleaseError(f"activation probe returned an invalid {label} SHA-256")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ReleaseError(f"activation probe returned invalid {label} base64") from exc
    if len(content) != size:
        raise ReleaseError(f"activation probe {label} size does not match its bytes")
    if sha256_bytes(content) != digest:
        raise ReleaseError(f"activation probe {label} SHA-256 does not match its bytes")
    return content


def _derive_release_activation_bundle(
    layout: ReleaseLayout,
    release_id: str,
    interpreter: Path,
) -> _ActivationBundle:
    """Derive canonical activation bytes from one descriptor-anchored wheel.

    Manifest and wheel bytes are read through an ``openat``/``O_NOFOLLOW``
    directory chain. A private verified copy is then imported under ``-I -B`` so
    a writable historical extracted site cannot create bytecode, redirect the
    probe through symlinks, or race the child between verification and import.
    """

    snapshot = _read_activation_artifact_snapshot(layout, release_id)
    try:
        resolved_interpreter = interpreter.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError(f"could not resolve activation Python interpreter: {exc}") from exc
    _regular_file_hash(
        resolved_interpreter,
        label="activation Python interpreter",
        executable=True,
    )
    probe = r'''
from __future__ import annotations
import base64
import hashlib
import json
import sys
from pathlib import Path

wheel = Path(sys.argv[1]).resolve(strict=True)
root = Path(sys.argv[2]).absolute()
bin_dir = Path(sys.argv[3]).absolute()
interpreter = Path(sys.argv[4]).resolve(strict=True)
release_id = sys.argv[5]
sys.path.insert(0, str(wheel))
import termdeck
import termdeck.release_manager as manager

module_path = str(manager.__file__)
package_path = str(termdeck.__file__)
expected_module = f"{wheel}/termdeck/release_manager.py"
expected_package = f"{wheel}/termdeck/__init__.py"
if module_path != expected_module or package_path != expected_package:
    raise RuntimeError(
        f"activation import escaped target wheel: {module_path!r}, {package_path!r}"
    )
layout = manager.release_layout(root=root, bin_dir=bin_dir)
bootstrap = manager._bootstrap_source(layout, interpreter)
launcher = manager._launcher_source(layout, interpreter)
if not isinstance(bootstrap, bytes) or not isinstance(launcher, bytes):
    raise RuntimeError("activation source functions did not return bytes")
payload = {
    "schema": "t1000.activation-probe.v1",
    "release_id": release_id,
    "module_path": module_path,
    "package_path": package_path,
    "managed_root": str(root),
    "bin_dir": str(bin_dir),
    "python_executable": str(interpreter),
    "bootstrap_base64": base64.b64encode(bootstrap).decode("ascii"),
    "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
    "bootstrap_size": len(bootstrap),
    "launcher_base64": base64.b64encode(launcher).decode("ascii"),
    "launcher_sha256": hashlib.sha256(launcher).hexdigest(),
    "launcher_size": len(launcher),
}
sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
'''
    probe_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TZ"}
    }
    probe_env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    with tempfile.TemporaryDirectory(prefix="t1000-activation-probe-") as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        wheel = temporary_root / snapshot.wheel_name
        manifest_path = temporary_root / "manifest.json"
        _atomic_write_bytes(wheel, snapshot.wheel_bytes, mode=0o400)
        _atomic_write_bytes(manifest_path, snapshot.manifest_bytes, mode=0o400)
        verified = verify_wheel(wheel, manifest_path=manifest_path)
        if verified.manifest is None or verified.manifest.get("release_id") != release_id:
            raise ReleaseError("activation target wheel and release id disagree")
        resolved_wheel = wheel.resolve(strict=True)
        expected_module = f"{resolved_wheel}/termdeck/release_manager.py"
        expected_package = f"{resolved_wheel}/termdeck/__init__.py"
        completed = _run_bounded_output(
            [
                str(resolved_interpreter),
                "-I",
                "-B",
                "-c",
                probe,
                str(resolved_wheel),
                str(layout.root),
                str(layout.bin_dir),
                str(resolved_interpreter),
                release_id,
            ],
            cwd=temporary_root,
            env=probe_env,
            timeout=60,
            stdout_limit=_MAX_ACTIVATION_PROBE_OUTPUT_BYTES,
            stderr_limit=_MAX_ACTIVATION_PROBE_STDERR_BYTES,
        )
        if completed.stderr:
            raise ReleaseError("activation probe emitted unexpected stderr")
        if not completed.stdout:
            raise ReleaseError("activation probe output is empty")
        try:
            output = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError("activation probe output was not valid UTF-8") from exc
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ReleaseError("activation probe output is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ReleaseError("activation probe output is not a JSON object")
        expected_fields = {
            "schema",
            "release_id",
            "module_path",
            "package_path",
            "managed_root",
            "bin_dir",
            "python_executable",
            "bootstrap_base64",
            "bootstrap_sha256",
            "bootstrap_size",
            "launcher_base64",
            "launcher_sha256",
            "launcher_size",
        }
        if set(payload) != expected_fields:
            raise ReleaseError("activation probe fields differ from the closed contract")
        canonical_output = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
        )
        if output != canonical_output:
            raise ReleaseError("activation probe output is not canonical JSON")
        expected_identity = {
            "schema": _ACTIVATION_PROBE_SCHEMA,
            "release_id": release_id,
            "module_path": expected_module,
            "package_path": expected_package,
            "managed_root": str(layout.root),
            "bin_dir": str(layout.bin_dir),
            "python_executable": str(resolved_interpreter),
        }
        for key, expected in expected_identity.items():
            if payload.get(key) != expected:
                raise ReleaseError(f"activation probe {key} does not match the target")
        bootstrap = _decode_activation_probe_source(payload, label="bootstrap")
        launcher = _decode_activation_probe_source(payload, label="launcher")
        return _activation_bundle(
            bootstrap,
            launcher,
            origin="selected-release",
        )


def _verified_release_activation_bundle(
    layout: ReleaseLayout,
    release_id: str,
    interpreter: Path,
) -> _ActivationBundle:
    _verify_release_dir(layout, release_id)
    return _derive_release_activation_bundle(layout, release_id, interpreter)


def _canonical_production_launcher_path() -> Path:
    from .desktop_integration import MANAGED_LAUNCHER

    return MANAGED_LAUNCHER


def _production_autostart_path() -> Path:
    from .desktop_integration import desktop_paths

    path = desktop_paths().autostart_file.absolute()
    if path.name != _AUTOSTART_DESCRIPTOR_NAME:
        raise ReleaseError("production autostart descriptor name is not canonical")
    return path


def _production_autostart_receipt_path(layout: ReleaseLayout) -> Path:
    return layout.root / _AUTOSTART_RECEIPT_NAME


def _production_manages_autostart(
    layout: ReleaseLayout,
    *,
    production: bool,
) -> bool:
    if not production or not _is_canonical_production_root(layout.root):
        return False
    expected_launcher = _canonical_production_launcher_path()
    if layout.launcher_path != expected_launcher:
        raise ReleaseError(
            "the canonical production root requires the stable launcher at "
            f"{expected_launcher}"
        )
    return True


def _canonical_production_autostart_descriptor(layout: ReleaseLayout) -> bytes:
    expected_launcher = _canonical_production_launcher_path()
    if layout.launcher_path != expected_launcher:
        raise ReleaseError("production autostart cannot target a non-canonical launcher")
    from .desktop_integration import _build_autostart_entry_for_launcher

    content = _build_autostart_entry_for_launcher(expected_launcher).encode("utf-8")
    if (
        not content
        or len(content) > _MAX_AUTOSTART_DESCRIPTOR_BYTES
        or b"\x00" in content
        or b"\r" in content
        or not content.endswith(b"\n")
    ):
        raise ReleaseError("production autostart descriptor bytes are not canonical LF-only text")
    lines = content.decode("utf-8").splitlines()
    required = {
        f"Exec={expected_launcher} gui",
        f"TryExec={expected_launcher}",
        f"X-T1000-Managed-Launcher={expected_launcher}",
    }
    if any(lines.count(line) != 1 for line in required):
        raise ReleaseError("production autostart descriptor has the wrong launcher contract")
    return content


def _bounded_file_snapshot(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> _FileSnapshot:
    if not _path_lexists(path):
        return _FileSnapshot(False)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseError(f"{label} is not a regular file: {path}")
    content = _read_regular_file_bytes(
        path,
        label=label,
        maximum_bytes=maximum_bytes,
    )
    try:
        after = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"could not revalidate {label}: {path}: {exc}") from exc
    identity = lambda entry: (
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_uid,
        entry.st_nlink,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise ReleaseError(f"{label} changed while being captured: {path}")
    return _FileSnapshot(True, content, stat.S_IMODE(after.st_mode))


def _private_regular_snapshot(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> _FileSnapshot:
    snapshot = _bounded_file_snapshot(
        path,
        label=label,
        maximum_bytes=maximum_bytes,
    )
    if not snapshot.exists:
        return snapshot
    metadata = path.lstat()
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or snapshot.mode != 0o600
    ):
        raise ReleaseError(f"{label} is not a private user-owned 0600 file: {path}")
    return snapshot


def _validate_autostart_prior_descriptor(
    value: object,
    *,
    layout: ReleaseLayout,
    descriptor_path: Path,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReleaseError("autostart receipt prior descriptor is invalid")
    prior = {str(key): item for key, item in value.items()}
    if set(prior) != {"backup_path", "mode", "sha256", "size", "source_path"}:
        raise ReleaseError("autostart receipt prior descriptor fields are invalid")
    if prior["source_path"] != str(descriptor_path):
        raise ReleaseError("autostart receipt prior descriptor path mismatch")
    digest = prior["sha256"]
    size = prior["size"]
    mode = prior["mode"]
    backup_path = prior["backup_path"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ReleaseError("autostart receipt prior descriptor digest is invalid")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= _MAX_AUTOSTART_DESCRIPTOR_BYTES
    ):
        raise ReleaseError("autostart receipt prior descriptor size is invalid")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise ReleaseError("autostart receipt prior descriptor mode is invalid")
    if not isinstance(backup_path, str) or not Path(backup_path).is_absolute():
        raise ReleaseError("autostart receipt prior descriptor backup path is invalid")
    backup = Path(backup_path)
    try:
        backup.resolve(strict=True).relative_to(layout.legacy_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReleaseError(
            "autostart prior descriptor backup escaped the legacy directory"
        ) from exc
    snapshot = _private_regular_snapshot(
        backup,
        label="autostart prior descriptor backup",
        maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
    )
    if (
        not snapshot.exists
        or len(snapshot.content) != size
        or sha256_bytes(snapshot.content) != digest
    ):
        raise ReleaseError("autostart prior descriptor backup does not match its receipt")
    return {
        "backup_path": str(backup),
        "mode": mode,
        "sha256": digest,
        "size": size,
        "source_path": str(descriptor_path),
    }


def _validate_autostart_receipt(
    value: object,
    *,
    layout: ReleaseLayout,
    state: Mapping[str, object],
    descriptor_path: Path,
    descriptor: bytes,
    bind_state: bool = True,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReleaseError("production autostart receipt is not an object")
    receipt = {str(key): item for key, item in value.items()}
    expected_fields = {
        "schema",
        "status",
        "active_release_id",
        "state_generation",
        "state_sha256",
        "descriptor_path",
        "descriptor_sha256",
        "descriptor_size",
        "launcher_path",
        "launcher_sha256",
        "prior_descriptor",
    }
    if set(receipt) != expected_fields:
        raise ReleaseError("production autostart receipt fields differ from schema")
    if receipt["schema"] != _AUTOSTART_RECEIPT_SCHEMA:
        raise ReleaseError("production autostart receipt schema is unsupported")
    receipt_release_id = receipt["active_release_id"]
    if receipt_release_id is not None and (
        not isinstance(receipt_release_id, str)
        or _RELEASE_ID.fullmatch(receipt_release_id) is None
    ):
        raise ReleaseError("production autostart receipt active release is invalid")
    expected_status = "managed" if receipt_release_id is not None else "legacy-degraded"
    if receipt["status"] != expected_status:
        raise ReleaseError("production autostart receipt status mismatch")
    state_generation = receipt["state_generation"]
    if (
        isinstance(state_generation, bool)
        or not isinstance(state_generation, int)
        or state_generation < 0
    ):
        raise ReleaseError("production autostart receipt state generation is invalid")
    state_sha256 = receipt["state_sha256"]
    if (
        not isinstance(state_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", state_sha256) is None
    ):
        raise ReleaseError("production autostart receipt managed-state digest is invalid")
    if bind_state:
        if receipt_release_id != state.get("current_release_id"):
            raise ReleaseError("production autostart receipt active release mismatch")
        if state_generation != state.get("generation"):
            raise ReleaseError("production autostart receipt state generation mismatch")
        expected_state_sha256 = sha256_bytes(canonical_json_bytes(state))
        if state_sha256 != expected_state_sha256:
            raise ReleaseError("production autostart receipt managed-state digest mismatch")
    if receipt["descriptor_path"] != str(descriptor_path):
        raise ReleaseError("production autostart receipt descriptor path mismatch")
    descriptor_sha256 = sha256_bytes(descriptor)
    if receipt["descriptor_sha256"] != descriptor_sha256:
        raise ReleaseError("production autostart receipt descriptor digest mismatch")
    if receipt["descriptor_size"] != len(descriptor):
        raise ReleaseError("production autostart receipt descriptor size mismatch")
    if receipt["launcher_path"] != str(layout.launcher_path):
        raise ReleaseError("production autostart receipt launcher path mismatch")
    observed_launcher_sha256 = receipt["launcher_sha256"]
    if (
        not isinstance(observed_launcher_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", observed_launcher_sha256) is None
    ):
        raise ReleaseError("production autostart receipt launcher digest is invalid")
    if bind_state and observed_launcher_sha256 != state.get("launcher_sha256"):
        raise ReleaseError("production autostart receipt launcher digest mismatch")
    receipt["prior_descriptor"] = _validate_autostart_prior_descriptor(
        receipt["prior_descriptor"],
        layout=layout,
        descriptor_path=descriptor_path,
    )
    return {key: receipt[key] for key in sorted(receipt)}


def _read_autostart_receipt(
    layout: ReleaseLayout,
    *,
    state: Mapping[str, object],
    descriptor_path: Path,
    descriptor: bytes,
    bind_state: bool = True,
) -> dict[str, object]:
    receipt_path = _production_autostart_receipt_path(layout)
    snapshot = _private_regular_snapshot(
        receipt_path,
        label="production autostart receipt",
        maximum_bytes=_MAX_AUTOSTART_RECEIPT_BYTES,
    )
    if not snapshot.exists:
        raise ReleaseError("production autostart receipt is missing")
    try:
        value = json.loads(snapshot.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"production autostart receipt is invalid: {exc}") from exc
    if snapshot.content != canonical_json_bytes(value):
        raise ReleaseError("production autostart receipt is not canonical")
    return _validate_autostart_receipt(
        value,
        layout=layout,
        state=state,
        descriptor_path=descriptor_path,
        descriptor=descriptor,
        bind_state=bind_state,
    )


def _backup_autostart_descriptor(
    layout: ReleaseLayout,
    descriptor_path: Path,
    *,
    snapshot: _FileSnapshot | None = None,
) -> dict[str, object] | None:
    if snapshot is None:
        snapshot = _bounded_file_snapshot(
            descriptor_path,
            label="existing production autostart descriptor",
            maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
        )
    if not snapshot.exists:
        return None
    metadata = descriptor_path.lstat()
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or snapshot.mode & 0o022
    ):
        raise ReleaseError("existing production autostart descriptor is not safely owned")
    digest = sha256_bytes(snapshot.content)
    layout.legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_mode = layout.legacy_dir.lstat().st_mode
    if stat.S_ISLNK(legacy_mode) or not stat.S_ISDIR(legacy_mode):
        raise ReleaseError("legacy descriptor directory is not a real directory")
    backup = layout.legacy_dir / f"autostart-{digest}.desktop"
    if _path_lexists(backup):
        existing = _private_regular_snapshot(
            backup,
            label="autostart prior descriptor backup",
            maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
        )
        if not existing.exists or existing.content != snapshot.content:
            raise ReleaseError("autostart prior descriptor backup path has different bytes")
    else:
        _atomic_write_bytes(backup, snapshot.content, mode=0o600)
    return {
        "backup_path": str(backup),
        "mode": snapshot.mode,
        "sha256": digest,
        "size": len(snapshot.content),
        "source_path": str(descriptor_path),
    }


def _autostart_descriptor_matches(path: Path, expected: bytes) -> bool:
    snapshot = _bounded_file_snapshot(
        path,
        label="production autostart descriptor",
        maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
    )
    if not snapshot.exists:
        return False
    metadata = path.lstat()
    return bool(
        snapshot.content == expected
        and snapshot.mode == 0o644
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
    )


def _prepare_production_autostart_publication(
    layout: ReleaseLayout,
    *,
    prior_state: Mapping[str, object],
    state: Mapping[str, object],
    recover_missing_receipt: bool = False,
) -> _AutostartPublication:
    descriptor_path = _production_autostart_path()
    receipt_path = _production_autostart_receipt_path(layout)
    descriptor = _canonical_production_autostart_descriptor(layout)
    descriptor_preimage = _bounded_file_snapshot(
        descriptor_path,
        label="existing production autostart descriptor",
        maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
    )
    receipt_preimage = _private_regular_snapshot(
        receipt_path,
        label="production autostart receipt",
        maximum_bytes=_MAX_AUTOSTART_RECEIPT_BYTES,
    )
    existing_receipt: dict[str, object] | None = None
    if receipt_preimage.exists:
        existing_receipt = _read_autostart_receipt(
            layout,
            state=prior_state,
            descriptor_path=descriptor_path,
            descriptor=descriptor,
        )
        if receipt_preimage.content != canonical_json_bytes(existing_receipt):
            raise ReleaseError("production autostart receipt changed while being prepared")
        prior_descriptor = existing_receipt["prior_descriptor"]
    else:
        if int(prior_state.get("generation", 0)) > 0:
            if not recover_missing_receipt:
                raise ReleaseError(
                    "production autostart receipt is missing for an existing managed generation"
                )
            # Receipt recovery is a cutover repair for the release that is
            # already selected.  A new release must use the ordinary,
            # receipt-authenticated install path so a missing receipt cannot
            # be used to adopt an unrelated activation.
            if prior_state.get("current_release_id") != state.get("current_release_id"):
                raise ReleaseError(
                    "autostart receipt recovery requires an idempotent managed release"
                )
        prior_descriptor = _backup_autostart_descriptor(
            layout,
            descriptor_path,
            snapshot=descriptor_preimage,
        )
    receipt = _validate_autostart_receipt(
        {
            "schema": _AUTOSTART_RECEIPT_SCHEMA,
            "status": (
                "managed" if state.get("current_release_id") is not None else "legacy-degraded"
            ),
            "active_release_id": state.get("current_release_id"),
            "state_generation": state.get("generation"),
            "state_sha256": sha256_bytes(canonical_json_bytes(state)),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": sha256_bytes(descriptor),
            "descriptor_size": len(descriptor),
            "launcher_path": str(layout.launcher_path),
            "launcher_sha256": state.get("launcher_sha256"),
            "prior_descriptor": prior_descriptor,
        },
        layout=layout,
        state=state,
        descriptor_path=descriptor_path,
        descriptor=descriptor,
    )
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_matches = bool(
        existing_receipt is not None
        and canonical_json_bytes(existing_receipt) == receipt_bytes
    )
    return _AutostartPublication(
        descriptor_path=descriptor_path,
        receipt_path=receipt_path,
        descriptor=descriptor,
        receipt=receipt_bytes,
        descriptor_preimage=descriptor_preimage,
        receipt_preimage=receipt_preimage,
        repaired=(
            not _autostart_descriptor_matches(descriptor_path, descriptor)
            or not receipt_matches
        ),
    )


def _verify_production_autostart(
    layout: ReleaseLayout,
    state: Mapping[str, object],
) -> dict[str, object]:
    descriptor_path = _production_autostart_path()
    descriptor = _canonical_production_autostart_descriptor(layout)
    snapshot = _bounded_file_snapshot(
        descriptor_path,
        label="production autostart descriptor",
        maximum_bytes=_MAX_AUTOSTART_DESCRIPTOR_BYTES,
    )
    if not snapshot.exists or snapshot.content != descriptor or snapshot.mode != 0o644:
        raise ReleaseError("production autostart descriptor does not match canonical bytes")
    metadata = descriptor_path.lstat()
    if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
        raise ReleaseError("production autostart descriptor is not safely user-owned")
    receipt = _read_autostart_receipt(
        layout,
        state=state,
        descriptor_path=descriptor_path,
        descriptor=descriptor,
    )
    launcher_sha256 = _regular_file_hash(
        layout.launcher_path,
        label="production autostart launcher",
        executable=True,
    )
    if launcher_sha256 != receipt["launcher_sha256"]:
        raise ReleaseError("production autostart launcher does not match its receipt")
    return {
        "status": receipt["status"],
        "active_release_id": receipt["active_release_id"],
        "descriptor_path": str(descriptor_path),
        "descriptor_sha256": receipt["descriptor_sha256"],
        "launcher_path": str(layout.launcher_path),
        "launcher_sha256": launcher_sha256,
        "receipt_path": str(_production_autostart_receipt_path(layout)),
        "state_generation": receipt["state_generation"],
        "prior_descriptor": receipt["prior_descriptor"],
    }


def _publish_production_autostart(
    publication: _AutostartPublication,
    *,
    layout: ReleaseLayout,
    state: Mapping[str, object],
    finalize: Callable[[], None] | None = None,
) -> dict[str, object]:
    from .desktop_integration import _publish_files_transactionally as publish_desktop_files

    verified: dict[str, object] | None = None

    def verify() -> None:
        nonlocal verified
        if finalize is not None:
            finalize()
        verified = _verify_production_autostart(layout, state)

    publish_desktop_files(
        (
            (
                publication.descriptor_path,
                publication.descriptor,
                0o644,
                "production autostart descriptor",
            ),
            (
                publication.receipt_path,
                publication.receipt,
                0o600,
                "production autostart receipt",
            ),
        ),
        finalize=verify,
        expected_preimages={
            publication.descriptor_path: (
                publication.descriptor_preimage.exists,
                publication.descriptor_preimage.content,
                publication.descriptor_preimage.mode,
            ),
            publication.receipt_path: (
                publication.receipt_preimage.exists,
                publication.receipt_preimage.content,
                publication.receipt_preimage.mode,
            ),
        },
    )
    if verified is None:
        raise ReleaseError("production autostart publication was not verified")
    return verified


def _backup_legacy_launcher(layout: ReleaseLayout) -> dict[str, object] | None:
    if not _path_lexists(layout.launcher_path):
        return None
    snapshot = _capture_file(layout.launcher_path, label="existing t1000 launcher")
    assert snapshot.exists
    digest = sha256_bytes(snapshot.content)
    layout.legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_mode = layout.legacy_dir.lstat().st_mode
    if stat.S_ISLNK(legacy_mode) or not stat.S_ISDIR(legacy_mode):
        raise ReleaseError("legacy launcher directory is not a real directory")
    legacy_root = layout.legacy_dir.resolve(strict=True)
    backup = legacy_root / f"t1000-{digest}"
    if _path_lexists(backup):
        observed = _regular_file_hash(backup, label="legacy launcher backup")
        if observed != digest:
            raise ReleaseError("legacy launcher backup path contains different bytes")
    else:
        _atomic_write_bytes(backup, snapshot.content, mode=snapshot.mode)
    return {"path": str(backup), "sha256": digest, "mode": snapshot.mode}


def _verify_release_dir(layout: ReleaseLayout, release_id: str) -> dict[str, object]:
    result, _site_manifest = _verify_release_dir_details(layout, release_id)
    return result


def _verify_release_dir_details(
    layout: ReleaseLayout,
    release_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseError("release id is unsafe")
    release_dir = layout.releases_dir / release_id
    result, site_manifest = _verify_installed_tree_details(release_dir)
    if result["release_id"] != release_id:
        raise ReleaseError("installed directory and manifest release ids disagree")
    return result, site_manifest


def _staging_inode(info: os.stat_result) -> tuple[int, int, int]:
    """Keep staging ownership stable across directory chmod and rename."""

    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _staged_release_guard(
    parent: _DirectoryAnchor,
    name: str,
    descriptor: int,
    *,
    phase: str,
) -> _DirectoryGuard:
    parent.assert_current(phase=phase)
    opened = os.fstat(descriptor)
    named = _lstat_at(parent.descriptor, name, label="staged release directory")
    if not stat.S_ISDIR(opened.st_mode) or _staging_inode(named) != _staging_inode(opened):
        raise ReleaseError(f"staged release directory identity changed {phase}")
    return _DirectoryGuard(
        parent.path / name,
        parent.chain + (_directory_identity(opened, label="staged release directory"),),
    )


def _restore_staging_entry(
    descriptor: int,
    source: str,
    destination: str,
    expected: os.stat_result,
) -> None:
    """Restore a displaced entry without overwriting a reoccupied name."""

    observed = _lstat_at(descriptor, source, label="staging restore source")
    if _staging_inode(observed) != _staging_inode(expected):
        raise ReleaseError("staging restore source changed; preserving both names")
    _renameat2(
        descriptor,
        source,
        destination,
        flags=_RENAME_NOREPLACE,
        label="staging entry restoration",
    )
    restored = _lstat_at(descriptor, destination, label="restored staging entry")
    if _staging_inode(restored) != _staging_inode(expected):
        raise ReleaseError("restored staging entry changed; preserving the moved entry")
    _fsync_descriptor(descriptor, label="restored staging entry parent")


def _remove_staging_directory_contents(
    descriptor: int,
    *,
    assert_owned: Callable[[], None],
) -> None:
    """Thaw opened directories and dispose children under private relative names.

    Descendants are checked when opened; this is not a creation journal for
    every extracted member. Unexpected types and observed inode changes are
    preserved. As with publication cleanup, final unlink/rmdir consumes a
    freshly randomized private name because Linux has no inode-conditional
    deletion primitive.
    """

    assert_owned()
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    os.fchmod(descriptor, 0o700)
    for name in names:
        assert_owned()
        before = _lstat_at(descriptor, name, label="failed staging member")
        is_directory = stat.S_ISDIR(before.st_mode)
        if not is_directory and (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        ):
            raise ReleaseError("failed staging tree contains an unexpected member; preserving it")
        flags = (
            _directory_open_flags()
            if is_directory
            else _file_open_flags() | getattr(os, "O_NONBLOCK", 0)
        )
        child_fd = _openat(descriptor, name, flags, label="failed staging member")
        private_name: str | None = None
        try:
            opened = os.fstat(child_fd)
            if _staging_inode(opened) != _staging_inode(before):
                raise ReleaseError("failed staging member changed while opening; preserving it")
            assert_owned()
            private_name = f".cleanup-{uuid.uuid4().hex}"
            _renameat2(
                descriptor,
                name,
                private_name,
                flags=_RENAME_NOREPLACE,
                label="failed staging member quarantine",
            )
            moved = _lstat_at(descriptor, private_name, label="quarantined staging member")
            if _staging_inode(moved) != _staging_inode(opened):
                _restore_staging_entry(descriptor, private_name, name, moved)
                private_name = None
                raise ReleaseError("failed staging member changed before quarantine; preserving it")

            def assert_child_owned() -> None:
                assert_owned()
                current = _lstat_at(descriptor, private_name, label="owned staging member")
                if _staging_inode(current) != _staging_inode(os.fstat(child_fd)):
                    raise ReleaseError("quarantined staging member changed; preserving it")

            if is_directory:
                _remove_staging_directory_contents(child_fd, assert_owned=assert_child_owned)
                assert_child_owned()
                os.rmdir(private_name, dir_fd=descriptor)
            else:
                current = os.fstat(child_fd)
                if not _same_renamed_file_identity(
                    _file_identity(current, label="failed staging member"),
                    _file_identity(before, label="failed staging member preimage"),
                ):
                    raise ReleaseError("failed staging file changed; preserving it")
                assert_child_owned()
                _unlinkat(descriptor, private_name, label="failed staging member")
            private_name = None
        except BaseException as exc:
            if private_name is not None:
                try:
                    _restore_staging_entry(descriptor, private_name, name, os.fstat(child_fd))
                except Exception as restore_exc:
                    exc.add_note(f"staging member was preserved after cleanup failed: {restore_exc}")
            raise
        finally:
            os.close(child_fd)
    _fsync_descriptor(descriptor, label="cleaned staging directory")


def _remove_staging_tree(
    layout: ReleaseLayout,
    stage: Path,
    *,
    release_id: str,
    parent: _DirectoryAnchor,
    stage_descriptor: int,
) -> None:
    """Remove a created stage only while its name matches the retained descriptor."""

    expected_prefix = f".staging-{release_id}-"
    if stage.parent != layout.releases_dir or not stage.name.startswith(expected_prefix):
        raise ReleaseError(f"refusing to remove an unowned staging path: {stage}")
    if parent.path != layout.releases_dir:
        raise ReleaseError("failed staging parent does not match the managed releases directory")
    try:
        _staged_release_guard(parent, stage.name, stage_descriptor, phase="before staging cleanup")
    except FileNotFoundError:
        return
    private_name = f".staging-{release_id}-cleanup-{uuid.uuid4().hex}"
    _renameat2(
        parent.descriptor,
        stage.name,
        private_name,
        flags=_RENAME_NOREPLACE,
        label="failed staging tree quarantine",
    )
    moved = _lstat_at(parent.descriptor, private_name, label="quarantined staging tree")
    if _staging_inode(moved) != _staging_inode(os.fstat(stage_descriptor)):
        _restore_staging_entry(parent.descriptor, private_name, stage.name, moved)
        raise ReleaseError("failed staging tree changed before quarantine; preserving it")

    def assert_owned() -> None:
        _staged_release_guard(parent, private_name, stage_descriptor, phase="during staging cleanup")

    try:
        _remove_staging_directory_contents(stage_descriptor, assert_owned=assert_owned)
        assert_owned()
        os.rmdir(private_name, dir_fd=parent.descriptor)
        parent.fsync(label="removed failed staging tree")
    except BaseException as exc:
        try:
            _restore_staging_entry(
                parent.descriptor, private_name, stage.name, os.fstat(stage_descriptor)
            )
        except Exception as restore_exc:
            exc.add_note(f"staging tree was preserved after cleanup failed: {restore_exc}")
        raise


def _stage_release(
    layout: ReleaseLayout,
    verified: VerifiedWheel,
    manifest_path: Path,
    *,
    capture_release_guard: Callable[[_DirectoryGuard], None] | None = None,
) -> Path:
    assert verified.manifest is not None
    _require_production_build(
        verified.manifest["build_info"],
        label="release artifact",
    )
    release_id = str(verified.manifest["release_id"])
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseError("release id is unsafe")
    parent = _open_directory_anchor(
        layout.releases_dir, label="managed releases directory", create_parents=True
    )
    final = layout.releases_dir / release_id
    stage = layout.releases_dir / f".staging-{release_id}-{os.getpid()}-{uuid.uuid4().hex}"
    stage_created = False
    stage_descriptor: int | None = None
    try:
        try:
            existing = _lstat_at(parent.descriptor, release_id, label="existing release")
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISDIR(existing.st_mode):
                raise ReleaseError("existing release is not a real directory")
            try:
                descriptor = _openat(
                    parent.descriptor, release_id, _directory_open_flags(), label="existing release"
                )
            except OSError as exc:
                raise ReleaseError(f"could not open existing release as a real directory: {exc}") from exc
            try:
                if _staging_inode(existing) != _staging_inode(os.fstat(descriptor)):
                    raise ReleaseError("existing release changed while opening")
                guard = _staged_release_guard(
                    parent, release_id, descriptor, phase="before existing release verification"
                )
                verification, site_manifest = _verify_release_dir_details(layout, release_id)
                if _staged_release_guard(
                    parent, release_id, descriptor, phase="after existing release verification"
                ) != guard:
                    raise ReleaseError("existing release changed while being verified")
                _write_release_verification_receipt(
                    layout,
                    release_id,
                    final,
                    verification,
                    site_manifest,
                    expected_release_guard=guard,
                )
                if _staged_release_guard(
                    parent, release_id, descriptor, phase="after existing release receipt"
                ) != guard:
                    raise ReleaseError("existing release changed while writing its receipt")
                if capture_release_guard is not None:
                    capture_release_guard(guard)
                return final
            finally:
                os.close(descriptor)
        wheel_content = _verified_wheel_bytes(verified)
        parent.assert_current(phase="before creating release stage")
        _mkdirat(parent.descriptor, stage.name, mode=0o755, label="release stage")
        stage_created = True
        created = _lstat_at(parent.descriptor, stage.name, label="created release stage")
        stage_descriptor = _openat(
            parent.descriptor, stage.name, _directory_open_flags(), label="created release stage"
        )
        if _staging_inode(created) != _staging_inode(os.fstat(stage_descriptor)):
            # The opened directory is not proven to be the one we observed
            # after creation. Never give cleanup ownership of this descriptor.
            os.close(stage_descriptor)
            stage_descriptor = None
            raise ReleaseError("created release stage changed while opening")
        _staged_release_guard(parent, stage.name, stage_descriptor, phase="before extraction")
        site = stage / "site"
        _extract_verified_wheel(verified, site, content=wheel_content)
        _staged_release_guard(parent, stage.name, stage_descriptor, phase="after extraction")
        artifacts = stage / "artifacts"
        artifacts.mkdir()
        _atomic_write_bytes(
            artifacts / verified.path.name,
            wheel_content,
            mode=0o444,
        )
        _atomic_write_bytes(
            stage / "manifest.json",
            canonical_json_bytes(verified.manifest),
            mode=0o444,
        )
        build_info = verified.manifest.get("build_info")
        if not isinstance(build_info, Mapping):
            raise ReleaseError("verified manifest has no build-info mapping")
        _staged_release_guard(parent, stage.name, stage_descriptor, phase="before freezing release")
        if _release_requires_frozen_directories(build_info):
            _freeze_release_tree(stage)
        frozen_guard = _staged_release_guard(
            parent, stage.name, stage_descriptor, phase="before staged release verification"
        )
        verification, site_manifest = _verify_installed_tree_details(stage)
        if _staged_release_guard(
            parent, stage.name, stage_descriptor, phase="before release publication"
        ) != frozen_guard:
            raise ReleaseError("staged release changed while being verified")
        try:
            _renameat2(
                parent.descriptor,
                stage.name,
                release_id,
                flags=_RENAME_NOREPLACE,
                label="release publication",
            )
        except FileExistsError as exc:
            raise ReleaseError("release destination appeared before staging publication") from exc
        published = _lstat_at(parent.descriptor, release_id, label="published release")
        if _staging_inode(published) != _staging_inode(os.fstat(stage_descriptor)):
            _restore_staging_entry(parent.descriptor, release_id, stage.name, published)
            raise ReleaseError("release publication source changed; preserving the moved entry")
        guard = _staged_release_guard(
            parent, release_id, stage_descriptor, phase="after release publication"
        )
        if guard.chain != frozen_guard.chain:
            raise ReleaseError("published release directory mode changed")
        parent.fsync(label="published release")
        _write_release_verification_receipt(
            layout,
            release_id,
            final,
            verification,
            site_manifest,
            expected_release_guard=guard,
        )
        if _staged_release_guard(
            parent, release_id, stage_descriptor, phase="after release receipt"
        ) != guard:
            raise ReleaseError("published release changed while writing its receipt")
        if capture_release_guard is not None:
            capture_release_guard(guard)
    except BaseException as exc:
        if stage_created and stage_descriptor is not None:
            try:
                _remove_staging_tree(
                    layout,
                    stage,
                    release_id=release_id,
                    parent=parent,
                    stage_descriptor=stage_descriptor,
                )
            except Exception as cleanup_exc:
                exc.add_note(f"failed release stage was preserved: {cleanup_exc}")
        raise
    finally:
        if stage_descriptor is not None:
            os.close(stage_descriptor)
        parent.close()
    return final


def _verify_staged_import(release_dir: Path, python: Path) -> None:
    """Import the staged bytes without depending on unpublished managed state."""

    code = (
        "import json,os,sys;"
        "sys.dont_write_bytecode=True;"
        "from pathlib import Path;"
        "release=Path(sys.argv[1]).resolve(strict=True);"
        "site=(release/'site').resolve(strict=True);"
        "sys.path.insert(0,str(site));"
        "manifest_path=(release/'manifest.json').resolve(strict=True);"
        "os.environ['T1000_RELEASE_MANIFEST']=str(manifest_path);"
        "from termdeck.build_info import load_bundled_build_info,read_json,validate_release_manifest;"
        "manifest=validate_release_manifest(read_json(manifest_path,label='staged manifest'));"
        "build=load_bundled_build_info();"
        "assert manifest['build_info']==build,(manifest,build);"
        "import termdeck,termdeck.cli;"
        "assert Path(termdeck.__file__).resolve().is_relative_to(site);"
        "assert Path(termdeck.cli.__file__).resolve().is_relative_to(site);"
        "print(json.dumps({'ok':True,'release_id':manifest['release_id'],'build_id':build['build_id']},sort_keys=True))"
    )
    _run(
        [str(python), "-I", "-B", "-c", code, str(release_dir)],
        cwd=release_dir,
        timeout=60,
    )


def install_release(
    wheel_path: str | Path,
    manifest_path: str | Path,
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    python: str | Path | None = None,
    lock_timeout: float = 15.0,
    production: bool | None = None,
    recover_autostart: bool = False,
    before_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    layout = release_layout(root=root, bin_dir=bin_dir)
    production = _resolve_production_intent(layout, production)
    manage_autostart = _production_manages_autostart(
        layout,
        production=production,
    )
    if recover_autostart and not manage_autostart:
        raise ReleaseError(
            "autostart recovery requires the canonical production root"
        )
    storage_attestation = _require_managed_storage(layout) if production else None
    storage_binding = (
        _storage_attestation_binding(storage_attestation)
        if storage_attestation is not None
        else None
    )
    manifest_source = Path(manifest_path).expanduser()
    verified = verify_wheel(wheel_path, manifest_path=manifest_source)
    assert verified.manifest is not None
    _require_production_build(
        verified.manifest["build_info"],
        label="release artifact",
    )
    release_id = str(verified.manifest["release_id"])
    interpreter = (
        Path(python).expanduser().resolve(strict=True)
        if python is not None
        else Path(sys.executable).resolve(strict=True)
    )
    interpreter_mode = interpreter.lstat().st_mode
    if (
        stat.S_ISLNK(interpreter_mode)
        or not stat.S_ISREG(interpreter_mode)
        or not os.access(interpreter, os.X_OK)
    ):
        raise ReleaseError(f"Python interpreter is not an executable regular file: {interpreter}")

    with release_lock(layout, timeout=lock_timeout):
        # Resume a prior process's combined wrapper/state intent before any
        # state-based decision.  This closes first-install's old state=None
        # ownership gap without asking a caller to infer wrapper provenance.
        _recover_install_publication_locked(layout)
        state = load_managed_state(layout)
        state_digest = _state_digest(layout.state_path)
        current = state.get("current_release_id")
        if current is not None:
            if state.get("python_executable") != str(interpreter):
                raise ReleaseError(
                    "an existing managed root cannot change Python interpreter in place"
                )
            if state.get("launcher_path") != str(layout.launcher_path):
                raise ReleaseError(
                    "an existing managed root cannot change its stable launcher path"
                )

        current_activation = _current_activation_bundle(layout, interpreter)
        expected_launcher = current_activation.launcher
        expected_launcher_hash = current_activation.launcher_sha256
        expected_bootstrap = current_activation.bootstrap
        expected_bootstrap_hash = current_activation.bootstrap_sha256
        legacy = state.get("legacy_launcher")
        prior_activation = (
            _state_activation_bundle(
                layout,
                state,
                interpreter,
                verify_selected_release=False,
            )
            if current is not None
            else None
        )
        existing_hash = _optional_regular_hash(
            layout.launcher_path,
            label="t1000 launcher",
            executable=True,
        )
        allowed_existing = {None, expected_launcher_hash}
        if prior_activation is not None:
            allowed_existing.add(prior_activation.launcher_sha256)
        elif isinstance(legacy, Mapping):
            allowed_existing.add(str(legacy.get("sha256")))
        if current is not None and existing_hash not in allowed_existing:
            raise ReleaseError("managed t1000 launcher changed outside the release manager")

        if storage_binding is not None:
            storage_attestation = _revalidate_managed_storage(
                layout,
                storage_binding,
                phase="inside the install lock before release mutation",
            )
        release_guards: list[_DirectoryGuard] = []
        release_dir = _stage_release(
            layout,
            verified,
            manifest_source,
            capture_release_guard=release_guards.append,
        )
        if len(release_guards) != 1 or release_guards[0].path != release_dir:
            raise ReleaseError("staged release has no exact directory ownership guard")
        release_guard = release_guards[0]

        def assert_release_current() -> None:
            anchor = _open_directory_anchor(
                release_dir,
                label="selected staged release",
                create_parents=False,
            )
            try:
                if anchor.guard != release_guard:
                    raise ReleaseError("staged release directory changed before activation")
                anchor.assert_current(phase="during staged release handoff")
            finally:
                anchor.close()

        assert_release_current()
        _verify_staged_import(release_dir, interpreter)
        assert_release_current()
        manifest_hash = _regular_file_hash(
            release_dir / "manifest.json",
            label="staged release manifest",
        )

        if legacy is None and current is None and existing_hash != expected_launcher_hash:
            legacy = _backup_legacy_launcher(layout)

        layout.root.mkdir(parents=True, exist_ok=True)
        layout.bin_dir.mkdir(parents=True, exist_ok=True)
        for directory, label in (
            (layout.root, "managed release root"),
            (layout.bin_dir, "managed launcher directory"),
        ):
            mode = directory.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReleaseError(f"{label} is not a real directory")

        service_wrapper_publication = _prepare_service_wrapper_publication(
            layout,
            current_release_id=(str(current) if current is not None else None),
            target_release_id=release_id,
        )

        bootstrap_hash = _optional_regular_hash(
            layout.bootstrap_path,
            label="managed bootstrap",
            executable=True,
        )
        current_launcher_hash = _optional_regular_hash(
            layout.launcher_path,
            label="t1000 launcher",
            executable=True,
        )
        allowed_prior = {None, expected_launcher_hash}
        if prior_activation is not None:
            allowed_prior.add(prior_activation.launcher_sha256)
        elif isinstance(legacy, Mapping):
            allowed_prior.add(str(legacy.get("sha256")))
        if current_launcher_hash not in allowed_prior:
            raise ReleaseError("t1000 launcher changed before atomic publication")

        repaired_bootstrap = bootstrap_hash != expected_bootstrap_hash
        repaired_launcher = current_launcher_hash != expected_launcher_hash
        idempotent = current == release_id
        state_needs_update = (
            not idempotent
            or state.get("manifest_sha256") != manifest_hash
            or state.get("python_executable") != str(interpreter)
            or state.get("bootstrap_sha256") != expected_bootstrap_hash
            or state.get("launcher_path") != str(layout.launcher_path)
            or state.get("launcher_sha256") != expected_launcher_hash
            or state.get("legacy_launcher") != legacy
        )
        if state_needs_update:
            new_state = validate_managed_state(
                {
                    "schema": MANAGED_STATE_SCHEMA,
                    "generation": int(state["generation"]) + 1,
                    "current_release_id": release_id,
                    "previous_release_id": (
                        current if current != release_id else state.get("previous_release_id")
                    ),
                    "manifest_sha256": manifest_hash,
                    "python_executable": str(interpreter),
                    "bootstrap_sha256": expected_bootstrap_hash,
                    "launcher_path": str(layout.launcher_path),
                    "launcher_sha256": expected_launcher_hash,
                    "legacy_launcher": legacy,
                }
            )
        else:
            new_state = state

        autostart_publication = (
            _prepare_production_autostart_publication(
                layout,
                prior_state=state,
                state=new_state,
                recover_missing_receipt=recover_autostart,
            )
            if manage_autostart
            else None
        )
        autostart_writes, autostart_preimages = _autostart_publication_files(
            autostart_publication
        )
        first_install_preimage = _prepare_first_install_preimage_publication(
            layout,
            state,
            service_wrapper_publication,
            autostart_publication=autostart_publication,
        )
        first_install_receipt = (
            _first_install_preimage_payload(
                layout,
                new_state,
                service_wrapper_publication,
                first_install_preimage,
                autostart_publication=autostart_publication,
            )
            if first_install_preimage is not None
            else None
        )
        if _state_digest(layout.state_path) != state_digest:
            raise ReleaseError("managed state changed during install; compare-and-swap refused")

        writes: list[tuple[Path, bytes | None, int, str]] = []
        if first_install_preimage is not None:
            if first_install_receipt is None:  # pragma: no cover - construction invariant
                raise ReleaseError("first-install preimage receipt is unavailable")
            writes.append(
                (
                    first_install_preimage.receipt_path,
                    first_install_receipt,
                    0o600,
                    "first-install preimage receipt",
                )
            )
        if repaired_bootstrap:
            writes.append(
                (layout.bootstrap_path, expected_bootstrap, 0o755, "managed bootstrap")
            )
        if repaired_launcher:
            writes.append(
                (layout.launcher_path, expected_launcher, 0o755, "t1000 launcher")
            )
        writes.extend(autostart_writes)
        if state_needs_update:
            # The state pointer selects B.  Publish it last so a crash can
            # never expose B without its durable empty-baseline receipt.
            writes.append(
                (
                    layout.state_path,
                    canonical_json_bytes(new_state),
                    0o600,
                    "managed state",
                )
            )

        autostart_verification: dict[str, object] | None = None

        def verify_publication() -> None:
            nonlocal storage_attestation
            nonlocal autostart_verification
            assert_release_current()
            if storage_binding is not None:
                storage_attestation = _revalidate_managed_storage(
                    layout,
                    storage_binding,
                    phase="during install publication",
                )
            observed_state = load_managed_state(layout)
            if observed_state != new_state:
                raise ReleaseError("published managed state differs from the transaction")
            _verify_managed_activation(
                layout,
                observed_state,
                verify_release=True,
            )
            if autostart_publication is not None:
                autostart_verification = _verify_production_autostart(
                    layout,
                    observed_state,
                )

        if storage_binding is not None:
            storage_attestation = _revalidate_managed_storage(
                layout,
                storage_binding,
                phase="immediately before install publication",
            )
        # Wrapper files, their legacy preimage receipt, the desktop pair, and
        # the state pointer are one descriptor-journaled set.  Do not let the
        # wrapper API finish and discard its journal before ``state.json``
        # selects the same generation: an interrupt in that old gap produced
        # unowned B wrappers on a first install.
        service_wrapper_result = _publish_install_wrapper_and_activation(
            service_wrapper_publication,
            writes,
            verify=verify_publication,
            first_install_preimage=first_install_preimage,
            activation_expected_preimages=autostart_preimages,
            journal_path=_install_publication_journal_path(layout),
            before_publication=before_publication,
            after_publication=after_publication,
            after_recovery=after_recovery,
            production_probe=production,
        )

        result = {
            "ok": True,
            "action": "release-install",
            "idempotent": idempotent,
            "repaired": (
                repaired_bootstrap
                or repaired_launcher
                or state_needs_update
                or bool(autostart_publication and autostart_publication.repaired)
                or service_wrapper_publication.repaired
            ),
            "release_id": release_id,
            "previous_release_id": new_state.get("previous_release_id"),
            "root": str(layout.root),
            "release_dir": str(release_dir),
            "launcher": str(layout.launcher_path),
            "state_generation": new_state["generation"],
            "live_processes_restarted": False,
        }
        result["service_wrappers"] = service_wrapper_result
        result["publication_receipt"] = service_wrapper_result["publication_receipt"]
        if autostart_verification is not None:
            result["autostart"] = autostart_verification
        if storage_attestation is not None:
            result["storage_attestation"] = storage_attestation.as_dict()
        return result


def _autostart_publication_files(
    publication: _AutostartPublication | None,
) -> tuple[
    tuple[tuple[Path, bytes | None, int, str], ...],
    dict[Path, _FileSnapshot],
]:
    """Keep desktop preimages and selecting state in one release journal."""

    if publication is None:
        return (), {}
    return (
        (
            (
                publication.descriptor_path,
                publication.descriptor,
                0o644,
                "production autostart descriptor",
            ),
            (
                publication.receipt_path,
                publication.receipt,
                0o600,
                "production autostart receipt",
            ),
        ),
        {
            publication.descriptor_path: publication.descriptor_preimage,
            publication.receipt_path: publication.receipt_preimage,
        },
    )


def rollback_release(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    release_id: str | None = None,
    to_legacy: bool = False,
    lock_timeout: float = 15.0,
    production: bool | None = None,
    before_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[PublicationTransactionReceipt], None] | None = None,
    after_recovery: Callable[[PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    layout = release_layout(root=root, bin_dir=bin_dir)
    production = _resolve_production_intent(layout, production)
    manage_autostart = _production_manages_autostart(
        layout,
        production=production,
    )
    storage_attestation = _require_managed_storage(layout) if production else None
    storage_binding = (
        _storage_attestation_binding(storage_attestation)
        if storage_attestation is not None
        else None
    )
    if release_id is not None and to_legacy:
        raise ReleaseError("choose either a managed release or --legacy, not both")
    with release_lock(layout, timeout=lock_timeout):
        _recover_install_publication_locked(layout)
        state = load_managed_state(layout)
        state_digest = _state_digest(layout.state_path)
        current = state.get("current_release_id")
        if current is None:
            raise ReleaseError("there is no managed current release to roll back")
        if storage_binding is not None:
            storage_attestation = _revalidate_managed_storage(
                layout,
                storage_binding,
                phase="inside the rollback lock before release mutation",
            )

        service_wrapper_publication = _prepare_service_wrapper_publication(
            layout,
            current_release_id=str(current),
            target_release_id=str(current),
        )

        if to_legacy:
            legacy = state.get("legacy_launcher")
            if legacy is None:
                if _state_digest(layout.state_path) != state_digest:
                    raise ReleaseError("managed state changed during rollback")
                if storage_binding is not None:
                    storage_attestation = _revalidate_managed_storage(
                        layout,
                        storage_binding,
                        phase="immediately before empty legacy rollback publication",
                    )

                def verify_empty_storage() -> None:
                    nonlocal storage_attestation
                    if storage_binding is not None:
                        storage_attestation = _revalidate_managed_storage(
                            layout,
                            storage_binding,
                            phase="during empty legacy rollback publication",
                        )

                result = _rollback_first_install_to_empty_legacy(
                    layout,
                    state,
                    service_wrapper_publication,
                    manage_autostart=manage_autostart,
                    after_remove=verify_empty_storage,
                    journal_path=_rollback_publication_journal_path(layout),
                    before_publication=before_publication,
                    after_publication=after_publication,
                    after_recovery=after_recovery,
                )
                result.update(
                    {
                        "action": "release-rollback",
                        "live_processes_restarted": False,
                    }
                )
                if storage_attestation is not None:
                    result["storage_attestation"] = storage_attestation.as_dict()
                return result
            if not isinstance(legacy, Mapping):
                raise ReleaseError("no pre-managed launcher was preserved")
            legacy_mode = int(legacy["mode"])
            if legacy_mode & 0o111 == 0:
                raise ReleaseError("preserved legacy launcher is not executable")
            legacy_root_mode = layout.legacy_dir.lstat().st_mode
            if stat.S_ISLNK(legacy_root_mode) or not stat.S_ISDIR(legacy_root_mode):
                raise ReleaseError("legacy launcher directory is not a real directory")
            legacy_root = layout.legacy_dir.resolve(strict=True)
            backup = Path(str(legacy["path"]))
            backup_hash = _regular_file_hash(
                backup,
                label="legacy launcher backup",
                executable=True,
            )
            try:
                backup.resolve(strict=True).relative_to(legacy_root)
            except ValueError as exc:
                raise ReleaseError("legacy launcher backup escaped the managed legacy directory") from exc
            if backup_hash != legacy["sha256"]:
                raise ReleaseError("legacy launcher backup failed SHA-256 verification")
            new_state = validate_managed_state(
                {
                    "schema": MANAGED_STATE_SCHEMA,
                    "generation": int(state["generation"]) + 1,
                    "current_release_id": None,
                    "previous_release_id": current,
                    "manifest_sha256": "",
                    "python_executable": "",
                    "bootstrap_sha256": "",
                    "launcher_path": str(layout.launcher_path),
                    "launcher_sha256": str(legacy["sha256"]),
                    "legacy_launcher": dict(legacy),
                }
            )
            autostart_publication = (
                _prepare_production_autostart_publication(
                    layout,
                    prior_state=state,
                    state=new_state,
                )
                if manage_autostart
                else None
            )
            autostart_writes, autostart_preimages = _autostart_publication_files(
                autostart_publication
            )
            if _state_digest(layout.state_path) != state_digest:
                raise ReleaseError("managed state changed during rollback")

            autostart_verification: dict[str, object] | None = None

            def verify_legacy_publication() -> None:
                nonlocal storage_attestation
                nonlocal autostart_verification
                if storage_binding is not None:
                    storage_attestation = _revalidate_managed_storage(
                        layout,
                        storage_binding,
                        phase="during legacy rollback publication",
                    )
                observed = load_managed_state(layout)
                if observed != new_state:
                    raise ReleaseError("published legacy rollback state differs from transaction")
                _verify_managed_activation(layout, observed, verify_release=False)
                if autostart_publication is not None:
                    autostart_verification = _verify_production_autostart(
                        layout,
                        observed,
                    )

            if storage_binding is not None:
                storage_attestation = _revalidate_managed_storage(
                    layout,
                    storage_binding,
                    phase="immediately before legacy rollback publication",
                )
            legacy_wrapper_snapshot = _load_legacy_wrapper_snapshot(
                service_wrapper_publication
            )
            # Keep the legacy wrapper inverse, launcher, desktop pair, and
            # selecting state in one retained descriptor journal.  Interrupted
            # publication restores only its exact B preimages, preserving any
            # foreign winner and keeping the desktop receipt bound to state.
            service_wrapper_result = _publish_install_wrapper_and_activation(
                service_wrapper_publication,
                [
                    (
                        layout.launcher_path,
                        backup.read_bytes(),
                        legacy_mode,
                        "t1000 launcher",
                    ),
                    *autostart_writes,
                    (
                        layout.state_path,
                        canonical_json_bytes(new_state),
                        0o600,
                        "managed state",
                    ),
                ],
                verify=verify_legacy_publication,
                first_install_preimage=None,
                activation_expected_preimages=autostart_preimages,
                wrapper_snapshot=legacy_wrapper_snapshot,
                wrapper_expected_release_id=None,
                journal_path=_rollback_publication_journal_path(layout),
                before_publication=before_publication,
                after_publication=after_publication,
                after_recovery=after_recovery,
            )
            result = {
                "ok": True,
                "action": "release-rollback",
                "target": "legacy-launcher",
                "previous_release_id": current,
                "state_generation": new_state["generation"],
                "live_processes_restarted": False,
            }
            result["service_wrappers"] = service_wrapper_result
            result["publication_receipt"] = service_wrapper_result["publication_receipt"]
            if autostart_verification is not None:
                result["autostart"] = autostart_verification
            if storage_attestation is not None:
                result["storage_attestation"] = storage_attestation.as_dict()
            return result

        target = release_id or state.get("previous_release_id")
        if not isinstance(target, str) or not _RELEASE_ID.fullmatch(target):
            raise ReleaseError("no verified previous managed release is available")
        if target == current:
            raise ReleaseError("rollback target is already current")
        target_verification, target_site_manifest = _verify_release_dir_details(
            layout, target
        )
        _write_release_verification_receipt(
            layout,
            target,
            layout.releases_dir / target,
            target_verification,
            target_site_manifest,
        )
        interpreter = Path(str(state.get("python_executable", "")))
        _regular_file_hash(
            interpreter,
            label="managed Python interpreter",
            executable=True,
        )
        try:
            resolved_interpreter = interpreter.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError(f"could not resolve managed Python interpreter: {exc}") from exc
        if str(resolved_interpreter) != str(state.get("python_executable")):
            raise ReleaseError("managed Python interpreter path is not canonical")
        target_activation = _derive_release_activation_bundle(
            layout,
            target,
            resolved_interpreter,
        )
        manifest_hash = _regular_file_hash(
            layout.releases_dir / target / "manifest.json",
            label="rollback target manifest",
        )
        new_state = validate_managed_state(
            {
                "schema": MANAGED_STATE_SCHEMA,
                "generation": int(state["generation"]) + 1,
                "current_release_id": target,
                "previous_release_id": current,
                "manifest_sha256": manifest_hash,
                "python_executable": str(resolved_interpreter),
                "bootstrap_sha256": target_activation.bootstrap_sha256,
                "launcher_path": str(layout.launcher_path),
                "launcher_sha256": target_activation.launcher_sha256,
                "legacy_launcher": state.get("legacy_launcher"),
            }
        )
        autostart_publication = (
            _prepare_production_autostart_publication(
                layout,
                prior_state=state,
                state=new_state,
            )
            if manage_autostart
            else None
        )
        autostart_writes, autostart_preimages = _autostart_publication_files(
            autostart_publication
        )
        if _state_digest(layout.state_path) != state_digest:
            raise ReleaseError("managed state changed during rollback")

        autostart_verification: dict[str, object] | None = None

        def verify_managed_publication() -> None:
            nonlocal storage_attestation
            nonlocal autostart_verification
            if storage_binding is not None:
                storage_attestation = _revalidate_managed_storage(
                    layout,
                    storage_binding,
                    phase="during managed rollback publication",
                )
            observed = load_managed_state(layout)
            if observed != new_state:
                raise ReleaseError("published managed rollback state differs from transaction")
            _verify_managed_activation(
                layout,
                observed,
                verify_release=True,
                expected_activation=target_activation,
            )
            if autostart_publication is not None:
                autostart_verification = _verify_production_autostart(
                    layout,
                    observed,
                )

        if storage_binding is not None:
            storage_attestation = _revalidate_managed_storage(
                layout,
                storage_binding,
                phase="immediately before managed rollback publication",
            )
        # Bind B-to-A wrappers, activation files, desktop pair, and selecting
        # state in the same descriptor journal so compensation also restores
        # the desktop receipt to the generation selected by restored state.
        service_wrapper_publication = _prepare_service_wrapper_publication(
            layout,
            current_release_id=str(current),
            target_release_id=target,
        )
        service_wrapper_result = _publish_install_wrapper_and_activation(
            service_wrapper_publication,
            [
                (
                    layout.bootstrap_path,
                    target_activation.bootstrap,
                    0o755,
                    "managed bootstrap",
                ),
                (
                    layout.launcher_path,
                    target_activation.launcher,
                    0o755,
                    "t1000 launcher",
                ),
                *autostart_writes,
                (
                    layout.state_path,
                    canonical_json_bytes(new_state),
                    0o600,
                    "managed state",
                ),
            ],
            verify=verify_managed_publication,
            first_install_preimage=None,
            activation_expected_preimages=autostart_preimages,
            journal_path=_rollback_publication_journal_path(layout),
            before_publication=before_publication,
            after_publication=after_publication,
            after_recovery=after_recovery,
        )
        result = {
            "ok": True,
            "action": "release-rollback",
            "release_id": target,
            "previous_release_id": current,
            "state_generation": new_state["generation"],
            "activation_origin": target_activation.origin,
            "live_processes_restarted": False,
        }
        result["service_wrappers"] = service_wrapper_result
        result["publication_receipt"] = service_wrapper_result["publication_receipt"]
        if autostart_verification is not None:
            result["autostart"] = autostart_verification
        if storage_attestation is not None:
            result["storage_attestation"] = storage_attestation.as_dict()
        return result


def _verify_managed_activation(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    verify_release: bool,
    expected_activation: _ActivationBundle | None = None,
) -> dict[str, object]:
    """Verify the launcher/bootstrap/interpreter and selected release as one unit.

    A normal installation must match the current release-manager templates. A
    cross-version rollback may instead match the exact templates deterministically
    derived from the selected, fully verified release. State-recorded hashes are
    never accepted as their own authority.
    """

    configured_launcher = Path(str(state.get("launcher_path", "")))
    if configured_launcher != layout.launcher_path:
        raise ReleaseError("managed state launcher_path does not match this installation")
    launcher_hash = _regular_file_hash(
        configured_launcher,
        label="managed launcher",
        executable=True,
    )
    if launcher_hash != state.get("launcher_sha256"):
        raise ReleaseError("managed launcher SHA-256 does not match state.json")

    current = state.get("current_release_id")
    if current is None:
        service_wrappers = _verify_service_wrappers(layout, None)
        return {
            "current_release_id": None,
            "launcher_sha256": launcher_hash,
            "legacy": True,
            "service_wrappers": service_wrappers,
        }
    if not isinstance(current, str) or not _RELEASE_ID.fullmatch(current):
        raise ReleaseError("managed current release id is invalid")

    manifest_path = layout.releases_dir / current / "manifest.json"
    manifest_bytes = _read_regular_file_bytes(
        manifest_path,
        label="managed release manifest",
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = validate_release_manifest(
            json.loads(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, BuildIdentityError) as exc:
        raise ReleaseError(f"managed release manifest is invalid: {exc}") from exc
    if manifest_bytes != canonical_json_bytes(manifest):
        raise ReleaseError("managed release manifest is not canonical")
    _require_production_build(manifest["build_info"], label="managed release")

    interpreter = Path(str(state.get("python_executable", "")))
    _regular_file_hash(interpreter, label="managed Python interpreter", executable=True)
    try:
        resolved_interpreter = interpreter.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError(f"could not resolve managed Python interpreter: {exc}") from exc
    if str(resolved_interpreter) != str(state.get("python_executable")):
        raise ReleaseError("managed Python interpreter path is not canonical")

    activation = _state_activation_bundle(
        layout,
        state,
        resolved_interpreter,
        expected_activation=expected_activation,
        verify_selected_release=verify_release,
    )

    bootstrap_hash = _regular_file_hash(
        layout.bootstrap_path,
        label="managed bootstrap",
        executable=True,
    )
    if bootstrap_hash != activation.bootstrap_sha256:
        raise ReleaseError("managed bootstrap SHA-256 does not match state.json")
    if launcher_hash != activation.launcher_sha256:
        raise ReleaseError("managed launcher SHA-256 does not match canonical activation")

    manifest_hash = _regular_file_hash(manifest_path, label="managed release manifest")
    if manifest_hash != state.get("manifest_sha256"):
        raise ReleaseError("managed release manifest SHA-256 does not match state.json")
    release = (
        _verify_release_dir(layout, current)
        if verify_release
        else {
            "release_id": current,
            "manifest_sha256": manifest_hash,
        }
    )
    if verify_release:
        _read_release_verification_receipt(
            layout,
            current,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            site_manifest_sha256=str(release["site_manifest_sha256"]),
        )
    service_wrappers = _verify_service_wrappers(layout, current)
    return {
        "current_release_id": current,
        "python_executable": str(resolved_interpreter),
        "bootstrap_sha256": bootstrap_hash,
        "launcher_sha256": launcher_hash,
        "activation_origin": activation.origin,
        "release": release,
        "legacy": False,
        "service_wrappers": service_wrappers,
    }


@dataclass(frozen=True)
class _ProcObservation:
    """Stable fields read from one same-UID ``/proc/<pid>`` directory."""

    pid: int
    process_start_ticks: int
    real_uid: int
    effective_uid: int
    argv: tuple[bytes, ...]
    cmdline_sha256: str
    executable: Path | None = None


def _read_boot_id(proc_root: Path) -> str:
    """Return the canonical Linux boot ID that scopes PID/start-tick identity."""

    boot_id = (proc_root / "sys" / "kernel" / "random" / "boot_id").read_text(
        encoding="ascii"
    ).strip()
    if _BOOT_ID.fullmatch(boot_id) is None:
        raise ValueError("boot_id is not a canonical lowercase UUID")
    return boot_id


def _same_proc_observation(
    left: _ProcObservation,
    right: _ProcObservation,
    *,
    include_executable: bool,
) -> bool:
    """Compare one process identity without trusting PID alone."""

    stable = (
        left.pid == right.pid
        and left.process_start_ticks == right.process_start_ticks
        and left.real_uid == right.real_uid
        and left.effective_uid == right.effective_uid
        and left.argv == right.argv
        and left.cmdline_sha256 == right.cmdline_sha256
    )
    if not stable:
        return False
    return not include_executable or left.executable == right.executable


def _parse_proc_start_ticks(raw: str) -> int:
    """Return Linux proc-stat field 22 without misparsing spaces in ``comm``."""

    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    try:
        value = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise ValueError("process stat has no valid start-time field") from exc
    if value < 0:
        raise ValueError("process start-time field is negative")
    return value


def _parse_proc_uids(raw: str) -> tuple[int, int]:
    for line in raw.splitlines():
        if not line.startswith("Uid:"):
            continue
        values = line.split(":", 1)[1].split()
        try:
            real_uid = int(values[0])
            effective_uid = int(values[1])
        except (IndexError, ValueError) as exc:
            raise ValueError("process status has an invalid Uid field") from exc
        if real_uid < 0 or effective_uid < 0:
            raise ValueError("process status has a negative Uid field")
        return real_uid, effective_uid
    raise ValueError("process status has no Uid field")


def _parse_proc_argv(raw: bytes) -> tuple[bytes, ...]:
    """Preserve internal empty argv elements while dropping the final terminator.

    Linux exposes argv as NUL-separated bytes. Filtering all empty fields would
    let an invocation such as ``[python, -I, bootstrap, ""]`` masquerade as the
    no-argument GUI form. Only the one conventional trailing NUL is structural;
    every earlier empty field remains part of process identity and classification.
    """

    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return tuple(parts)


def _read_proc_observation(
    process_dir: Path,
    *,
    include_executable: bool,
) -> _ProcObservation:
    try:
        pid = int(process_dir.name)
    except ValueError as exc:
        raise ValueError("process directory name is not numeric") from exc
    stat_raw = (process_dir / "stat").read_text(encoding="utf-8")
    status_raw = (process_dir / "status").read_text(encoding="utf-8")
    cmdline = (process_dir / "cmdline").read_bytes()
    real_uid, effective_uid = _parse_proc_uids(status_raw)
    executable: Path | None = None
    if include_executable:
        executable = (process_dir / "exe").resolve(strict=True)
    return _ProcObservation(
        pid=pid,
        process_start_ticks=_parse_proc_start_ticks(stat_raw),
        real_uid=real_uid,
        effective_uid=effective_uid,
        argv=_parse_proc_argv(cmdline),
        cmdline_sha256=sha256_bytes(cmdline),
        executable=executable,
    )


def _consume_path_options(
    arguments: Sequence[bytes],
    *,
    allowed: frozenset[bytes],
) -> bool:
    """Recognise argparse path options without retaining their private values."""

    seen: set[bytes] = set()
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option: bytes | None = None
        inline_value: bytes | None = None
        for candidate in allowed:
            if token == candidate:
                option = candidate
                break
            prefix = candidate + b"="
            if token.startswith(prefix):
                option = candidate
                inline_value = token[len(prefix) :]
                break
        if option is None or option in seen:
            return False
        seen.add(option)
        if inline_value is not None:
            if not inline_value:
                return False
            index += 1
            continue
        if index + 1 >= len(arguments) or not arguments[index + 1]:
            return False
        index += 2
    return True


def _managed_gui_invocation_kind(tail: tuple[bytes, ...]) -> str | None:
    """Classify only CLI forms that deterministically dispatch to ``cmd_gui``."""

    if not tail:
        return "default-gui"
    if tail[0] == b"gui":
        if _consume_path_options(
            tail[1:],
            allowed=frozenset({b"--db", b"--workspace"}),
        ):
            return "explicit-gui"
        return None
    if _consume_path_options(tail, allowed=frozenset({b"--workspace"})):
        return "workspace-gui"
    return None


def _process_issue_list(counts: Mapping[str, int]) -> list[dict[str, object]]:
    return [
        {"code": code, "count": int(count)}
        for code, count in sorted(counts.items())
        if count > 0
    ]


def _process_posture(
    *,
    complete: bool,
    gui_count: int,
) -> tuple[str, dict[str, object]]:
    if not complete:
        return (
            "scan-incomplete",
            {
                "safe": False,
                "decision": "refuse-incomplete",
                "requires_shutdown": False,
            },
        )
    if gui_count == 0:
        return (
            "stopped",
            {"safe": True, "decision": "start", "requires_shutdown": False},
        )
    if gui_count == 1:
        return (
            "single-gui",
            {
                "safe": True,
                "decision": "replace-single",
                "requires_shutdown": True,
            },
        )
    return (
        "multiple-gui",
        {
            "safe": False,
            "decision": "refuse-multiple",
            "requires_shutdown": False,
        },
    )


def _scan_managed_gui_processes(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    proc_root: Path,
    expected_uid: int,
) -> dict[str, object]:
    """Read managed GUI identity without signalling or mutating any process.

    Numeric proc directories owned by another user are ignored before file
    reads. Same-user disappearances and PID reuse are expected scan races;
    unreadable or malformed same-user records make the result fail closed.
    """

    current = state.get("current_release_id")
    configured = isinstance(current, str) and bool(current)
    base: dict[str, object] = {
        "schema": _MANAGED_GUI_PROCESS_SCHEMA,
        "managed_release_configured": configured,
        "expected_uid": expected_uid,
        "boot_id": "",
        "expected_identity": {
            "python_executable": "",
            "bootstrap_path": "",
            "argv_prefix_sha256": "",
        },
        "complete": True,
        "posture": "stopped",
        "gui_count": 0,
        "processes": [],
        "activation": {
            "safe": True,
            "decision": "start",
            "requires_shutdown": False,
        },
        "scanned_pid_count": 0,
        "same_uid_pid_count": 0,
        "ignored_process_count": 0,
        "vanished_process_count": 0,
        "raced_process_count": 0,
        "issues": [],
    }
    if not configured:
        return base

    issue_counts: dict[str, int] = {}

    def issue(code: str) -> None:
        issue_counts[code] = issue_counts.get(code, 0) + 1

    if expected_uid < 0:
        issue("expected-uid-invalid")
        posture, activation = _process_posture(complete=False, gui_count=0)
        base.update(
            {
                "complete": False,
                "posture": posture,
                "activation": activation,
                "issues": _process_issue_list(issue_counts),
            }
        )
        return base

    try:
        root_stat = proc_root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("proc root is not a real directory")
        process_dirs = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except (OSError, ValueError):
        issue("proc-root-unavailable")
        posture, activation = _process_posture(complete=False, gui_count=0)
        base.update(
            {
                "complete": False,
                "posture": posture,
                "activation": activation,
                "issues": _process_issue_list(issue_counts),
            }
        )
        return base

    try:
        configured_python = Path(str(state.get("python_executable", ""))).resolve(
            strict=True
        )
        configured_bootstrap = layout.bootstrap_path.resolve(strict=True)
    except (OSError, RuntimeError):
        issue("expected-identity-unavailable")
        posture, activation = _process_posture(complete=False, gui_count=0)
        base.update(
            {
                "complete": False,
                "posture": posture,
                "activation": activation,
                "issues": _process_issue_list(issue_counts),
            }
        )
        return base

    legacy_prefix = (
        os.fsencode(str(configured_python)),
        b"-I",
        os.fsencode(str(configured_bootstrap)),
    )
    no_bytecode_prefix = (
        os.fsencode(str(configured_python)),
        b"-I",
        b"-B",
        os.fsencode(str(configured_bootstrap)),
    )
    accepted_prefixes = (
        ("isolated-no-bytecode", no_bytecode_prefix),
        ("legacy-isolated", legacy_prefix),
    )
    base["expected_identity"] = {
        "python_executable": str(configured_python),
        "bootstrap_path": str(configured_bootstrap),
        "argv_prefix_sha256": sha256_bytes(
            b"\0".join(no_bytecode_prefix) + b"\0"
        ),
        "accepted_argv_prefixes": [
            {
                "launch_profile": profile,
                "argv_prefix_sha256": sha256_bytes(b"\0".join(prefix) + b"\0"),
            }
            for profile, prefix in accepted_prefixes
        ],
    }
    try:
        boot_id = _read_boot_id(proc_root)
    except FileNotFoundError:
        issue("boot-id-unavailable")
        boot_id = ""
    except (OSError, UnicodeError):
        issue("boot-id-unavailable")
        boot_id = ""
    except ValueError:
        issue("boot-id-invalid")
        boot_id = ""
    if not boot_id:
        posture, activation = _process_posture(complete=False, gui_count=0)
        base.update(
            {
                "boot_id": "",
                "complete": False,
                "posture": posture,
                "activation": activation,
                "issues": _process_issue_list(issue_counts),
            }
        )
        return base
    base["boot_id"] = boot_id

    processes: list[dict[str, object]] = []
    scanned = same_uid = ignored = vanished = raced = 0
    for process_dir in process_dirs:
        scanned += 1
        try:
            entry_stat = process_dir.lstat()
        except FileNotFoundError:
            vanished += 1
            continue
        except OSError:
            issue("proc-entry-unreadable")
            continue
        if entry_stat.st_uid != expected_uid:
            ignored += 1
            continue
        same_uid += 1
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
            issue("same-uid-entry-invalid")
            continue
        try:
            first = _read_proc_observation(process_dir, include_executable=False)
        except (FileNotFoundError, ProcessLookupError):
            vanished += 1
            continue
        except PermissionError:
            issue("same-uid-entry-unreadable")
            continue
        except (OSError, UnicodeError, ValueError):
            issue("same-uid-entry-malformed")
            continue
        if first.real_uid != expected_uid or first.effective_uid != expected_uid:
            ignored += 1
            continue
        launch_profile: str | None = None
        prefix_length = 0
        for profile, candidate_prefix in accepted_prefixes:
            if first.argv[: len(candidate_prefix)] == candidate_prefix:
                launch_profile = profile
                prefix_length = len(candidate_prefix)
                break
        if launch_profile is None:
            ignored += 1
            continue
        invocation = _managed_gui_invocation_kind(first.argv[prefix_length:])
        if invocation is None:
            # Managed CLI invocations (including this release-info process) are
            # intentionally absent from the GUI inventory.
            ignored += 1
            continue
        try:
            second = _read_proc_observation(process_dir, include_executable=True)
        except (FileNotFoundError, ProcessLookupError):
            vanished += 1
            issue("same-uid-candidate-vanished")
            continue
        except PermissionError:
            issue("same-uid-candidate-unreadable")
            continue
        except (OSError, UnicodeError, ValueError):
            issue("same-uid-candidate-malformed")
            continue
        if not _same_proc_observation(first, second, include_executable=False):
            raced += 1
            issue("same-uid-candidate-raced")
            continue
        if second.executable != configured_python:
            ignored += 1
            continue
        try:
            confirmed = _read_proc_observation(process_dir, include_executable=True)
        except (FileNotFoundError, ProcessLookupError):
            vanished += 1
            issue("same-uid-candidate-vanished")
            continue
        except PermissionError:
            issue("same-uid-candidate-unreadable")
            continue
        except (OSError, UnicodeError, ValueError):
            issue("same-uid-candidate-malformed")
            continue
        if not _same_proc_observation(second, confirmed, include_executable=True):
            # A second stable full observation closes the torn-read window
            # between cmdline and /proc/<pid>/exe and rejects PID reuse or exec.
            raced += 1
            issue("same-uid-candidate-raced")
            continue
        processes.append(
            {
                "boot_id": boot_id,
                "pid": confirmed.pid,
                "process_start_ticks": confirmed.process_start_ticks,
                "uid": confirmed.effective_uid,
                "executable": str(confirmed.executable),
                "launch_profile": launch_profile,
                "invocation_kind": invocation,
                "argument_count": len(confirmed.argv),
                "cmdline_sha256": confirmed.cmdline_sha256,
            }
        )

    processes.sort(key=lambda item: (int(item["process_start_ticks"]), int(item["pid"])))
    complete = not issue_counts
    posture, activation = _process_posture(
        complete=complete,
        gui_count=len(processes),
    )
    base.update(
        {
            "complete": complete,
            "posture": posture,
            "gui_count": len(processes),
            "processes": processes,
            "activation": activation,
            "scanned_pid_count": scanned,
            "same_uid_pid_count": same_uid,
            "ignored_process_count": ignored,
            "vanished_process_count": vanished,
            "raced_process_count": raced,
            "issues": _process_issue_list(issue_counts),
        }
    )
    return base


def _process_scan_stable_projection(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Return only decision-relevant fields from one direct process scan."""

    return {field: payload.get(field) for field in _PROCESS_OBSERVER_STABLE_FIELDS}


def managed_gui_process_observer_receipt(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    proc_root: str | Path = "/proc",
    expected_uid: int | None = None,
) -> dict[str, object]:
    """Emit two stable direct scans for a brokered observer namespace.

    This function never dispatches another broker.  It is intentionally usable
    from an isolated Python helper launched by the systemd user manager, whose
    procfs permissions can differ from the original caller's containment.
    """

    layout = release_layout(root=root, bin_dir=bin_dir)
    state = load_managed_state(layout)
    current = state.get("current_release_id")
    if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
        raise ReleaseError("managed process observer requires a selected release")
    uid = os.getuid() if expected_uid is None else int(expected_uid)
    activation = _verify_managed_activation(layout, state, verify_release=True)
    if activation.get("current_release_id") != current:
        raise ReleaseError("managed process observer activation changed release")
    state_sha256 = sha256_bytes(canonical_json_bytes(state))
    first = _scan_managed_gui_processes(
        layout,
        state,
        proc_root=Path(proc_root),
        expected_uid=uid,
    )
    second = _scan_managed_gui_processes(
        layout,
        state,
        proc_root=Path(proc_root),
        expected_uid=uid,
    )
    observed_state = load_managed_state(layout)
    state_stable = observed_state == state
    if state_stable:
        observed_activation = _verify_managed_activation(
            layout,
            observed_state,
            verify_release=True,
        )
        state_stable = observed_activation.get("current_release_id") == current
    stable = _process_scan_stable_projection(first) == _process_scan_stable_projection(
        second
    )
    complete = bool(first.get("complete")) and bool(second.get("complete"))
    ok = state_stable and stable and complete
    error_code = ""
    if not state_stable:
        error_code = "managed-state-changed"
    elif not stable:
        error_code = "process-scan-unstable"
    elif not complete:
        error_code = "process-scan-incomplete"
    source_module = Path(__file__).resolve(strict=True)
    return {
        "schema": _PROCESS_OBSERVER_RECEIPT_SCHEMA,
        "ok": ok,
        "error_code": error_code,
        "observer": "systemd-user-manager",
        "source_root": str(source_module.parent.parent),
        "source_module": str(source_module),
        "root": str(layout.root),
        "bin_dir": str(layout.bin_dir),
        "current_release_id": current,
        "managed_state_sha256": state_sha256,
        "expected_uid": uid,
        "stable_samples": 2,
        "stable": stable,
        "state_stable": state_stable,
        "status": first,
    }


def _trusted_systemd_run() -> Path:
    candidate = shutil.which("systemd-run")
    if not candidate:
        raise ReleaseError("systemd-run is unavailable")
    try:
        executable = Path(candidate).resolve(strict=True)
        metadata = executable.stat()
        trusted_roots = tuple(
            path.resolve(strict=True) for path in (Path("/usr"), Path("/bin")) if path.exists()
        )
    except OSError as exc:
        raise ReleaseError("systemd-run cannot be inspected") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(executable, os.X_OK)
        or os.access(executable, os.W_OK)
        or not any(executable.is_relative_to(root) for root in trusted_roots)
    ):
        raise ReleaseError("systemd-run is not a trusted system executable")
    # User namespaces may expose host-root-owned files through the overflow UID,
    # so numeric owner 0 is not a portable trust predicate.  Instead bind trust
    # to a canonical system prefix and require that neither the executable nor
    # any directory beneath that prefix is writable by this observer.
    for root in trusted_roots:
        if not executable.is_relative_to(root):
            continue
        current = executable.parent
        while True:
            directory = current.stat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or directory.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or os.access(current, os.W_OK)
            ):
                raise ReleaseError("systemd-run system path is writable")
            if current == root:
                break
            current = current.parent
        break
    return executable


def _observer_client_environment(
    systemd_run: Path,
    *,
    expected_uid: int,
) -> dict[str, str]:
    """Construct a canonical user-manager client environment without ambient bus trust."""

    if expected_uid != os.geteuid():
        raise ReleaseError("process observer UID differs from the current user")
    try:
        account = pwd.getpwuid(expected_uid)
        runtime = Path("/run/user") / str(expected_uid)
    except KeyError as exc:
        raise ReleaseError("canonical user-manager environment is unavailable") from exc
    return _observer_client_environment_from_paths(
        systemd_run,
        expected_uid=expected_uid,
        account=account,
        runtime=runtime,
    )


def _observer_client_environment_from_paths(
    systemd_run: Path,
    *,
    expected_uid: int,
    account: object,
    runtime: Path,
) -> dict[str, str]:
    """Validate explicit user-manager paths before constructing its environment."""

    try:
        home = Path(str(getattr(account, "pw_dir"))).resolve(strict=True)
        runtime_metadata = runtime.lstat()
        bus = runtime / "bus"
        bus_metadata = bus.lstat()
    except (OSError, TypeError) as exc:
        raise ReleaseError("canonical user-manager environment is unavailable") from exc
    if (
        runtime.is_symlink()
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != expected_uid
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
    ):
        raise ReleaseError("canonical user runtime directory is unsafe")
    if (
        bus.is_symlink()
        or not stat.S_ISSOCK(bus_metadata.st_mode)
        or bus_metadata.st_uid != expected_uid
    ):
        raise ReleaseError("canonical user-manager bus is unsafe")
    environment = {
        "HOME": str(home),
        "USER": str(getattr(account, "pw_name")),
        "LOGNAME": str(getattr(account, "pw_name")),
        "SHELL": str(getattr(account, "pw_shell", "") or "/bin/sh"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
    }
    environment["PATH"] = os.pathsep.join(
        dict.fromkeys(
            (
                str(systemd_run.parent),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        )
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    return environment


def _receipt_contains_private_process_data(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in {"argv", "cmdline"}:
                return True
            if _receipt_contains_private_process_data(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_receipt_contains_private_process_data(child) for child in value)
    return False


def _validate_process_observer_receipt(
    payload: object,
    *,
    layout: ReleaseLayout,
    state: Mapping[str, object],
    expected_uid: int,
    direct: Mapping[str, object],
    source_module: Path,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ReleaseError("process observer receipt is not an object")
    current = state.get("current_release_id")
    expected_state_sha256 = sha256_bytes(canonical_json_bytes(state))
    expected_source_root = source_module.parent.parent
    required = {
        "schema": _PROCESS_OBSERVER_RECEIPT_SCHEMA,
        "ok": True,
        "error_code": "",
        "observer": "systemd-user-manager",
        "source_root": str(expected_source_root),
        "source_module": str(source_module),
        "root": str(layout.root),
        "bin_dir": str(layout.bin_dir),
        "current_release_id": current,
        "managed_state_sha256": expected_state_sha256,
        "expected_uid": expected_uid,
        "stable_samples": 2,
        "stable": True,
        "state_stable": True,
    }
    for key, expected in required.items():
        if payload.get(key) != expected:
            raise ReleaseError(f"process observer receipt failed field {key}")
    status = payload.get("status")
    if not isinstance(status, Mapping):
        raise ReleaseError("process observer receipt has no status object")
    if status.get("schema") != _MANAGED_GUI_PROCESS_SCHEMA:
        raise ReleaseError("process observer status schema is invalid")
    if status.get("complete") is not True or status.get("issues") != []:
        raise ReleaseError("process observer status is incomplete")
    if status.get("expected_uid") != expected_uid:
        raise ReleaseError("process observer status changed expected UID")
    if status.get("managed_release_configured") is not True:
        raise ReleaseError("process observer lost the selected release")
    if status.get("expected_identity") != direct.get("expected_identity"):
        raise ReleaseError("process observer changed expected process identity")
    direct_boot = direct.get("boot_id")
    if isinstance(direct_boot, str) and direct_boot and status.get("boot_id") != direct_boot:
        raise ReleaseError("process observer changed boot identity")
    broker_processes = status.get("processes")
    direct_processes = direct.get("processes")
    if not isinstance(broker_processes, list) or not isinstance(direct_processes, list):
        raise ReleaseError("process observer returned invalid process records")
    for process in direct_processes:
        if process not in broker_processes:
            raise ReleaseError("process observer omitted a directly verified process")
    if "observer" in status or _receipt_contains_private_process_data(payload):
        raise ReleaseError("process observer receipt exposed private process data")
    return dict(status)


def _process_observer_flight_key(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    expected_uid: int,
    direct: Mapping[str, object],
    source_module: Path,
) -> tuple[str, ...]:
    """Bind one in-progress flight to every shared observation assumption."""

    current = state.get("current_release_id")
    boot_id = direct.get("boot_id")
    expected_identity = direct.get("expected_identity")
    if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
        raise ReleaseError("process observer requires a selected release")
    if not isinstance(boot_id, str) or _BOOT_ID.fullmatch(boot_id) is None:
        raise ReleaseError("process observer direct boot identity is invalid")
    if not isinstance(expected_identity, Mapping):
        raise ReleaseError("process observer expected identity is invalid")
    return (
        str(layout.root),
        str(layout.bin_dir),
        str(expected_uid),
        sha256_bytes(canonical_json_bytes(state)),
        current,
        str(source_module),
        sha256_file(source_module),
        boot_id,
        sha256_bytes(canonical_json_bytes(expected_identity)),
    )


def _dispatch_process_observer_transport(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    expected_uid: int,
    source_module: Path,
) -> _ProcessObserverTransportResult:
    """Run one bounded broker transport without caller-specific validation."""

    try:
        current = state.get("current_release_id")
        if not isinstance(current, str) or _RELEASE_ID.fullmatch(current) is None:
            raise ReleaseError("process observer requires a selected release")
        before_state_sha256 = sha256_bytes(canonical_json_bytes(state))
        activation = _verify_managed_activation(layout, state, verify_release=True)
        if activation.get("current_release_id") != current:
            raise ReleaseError("process observer activation release mismatch")
        systemd_run = _trusted_systemd_run()
        source_root = source_module.parent.parent
        configured_python = Path(str(state.get("python_executable", ""))).resolve(
            strict=True
        )
        unit = f"t1000-process-observer-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        client_environment = _observer_client_environment(
            systemd_run,
            expected_uid=expected_uid,
        )
        home = client_environment["HOME"]
        command = [
            str(systemd_run),
            "--user",
            "--wait",
            "--collect",
            "--pipe",
            "--quiet",
            f"--unit={unit}",
            "--property=Type=exec",
            f"--property=RuntimeMaxSec={int(_PROCESS_OBSERVER_TIMEOUT_SECONDS)}s",
            "--property=TimeoutStopSec=2s",
            "--property=WorkingDirectory=/",
            f"--setenv=HOME={home}",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "--setenv=PYTHONHASHSEED=0",
            f"--setenv={_PROCESS_OBSERVER_ENV}=1",
            str(configured_python),
            "-I",
            "-B",
            "-c",
            _PROCESS_OBSERVER_HELPER_SOURCE,
            str(source_root),
            str(source_module),
            str(layout.root),
            str(layout.bin_dir),
            str(expected_uid),
        ]
        completed = _run_bounded_output(
            command,
            cwd=Path("/"),
            env=client_environment,
            timeout=_PROCESS_OBSERVER_TIMEOUT_SECONDS + 2.0,
            stdout_limit=_PROCESS_OBSERVER_STDOUT_BYTES,
            stderr_limit=_PROCESS_OBSERVER_STDERR_BYTES,
        )
    except (OSError, ReleaseError, RuntimeError, ValueError):
        return _ProcessObserverTransportResult("broker-dispatch-failed")
    if completed.returncode != 0:
        return _ProcessObserverTransportResult("broker-command-failed")
    if completed.stderr:
        return _ProcessObserverTransportResult("broker-stderr-not-empty")
    try:
        json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _ProcessObserverTransportResult("broker-output-invalid")
    try:
        observed_state = load_managed_state(layout)
        if sha256_bytes(canonical_json_bytes(observed_state)) != before_state_sha256:
            raise ReleaseError("managed state changed during process observation")
        observed_activation = _verify_managed_activation(
            layout,
            observed_state,
            verify_release=True,
        )
        if observed_activation.get("current_release_id") != current:
            raise ReleaseError("managed activation changed during process observation")
    except (OSError, ReleaseError, RuntimeError, ValueError):
        return _ProcessObserverTransportResult("broker-receipt-rejected")
    return _ProcessObserverTransportResult(
        "broker-output-ready",
        bytes(completed.stdout),
    )


def _publish_process_observer_flight(
    key: tuple[str, ...],
    flight: _ProcessObserverFlight,
    result: _ProcessObserverTransportResult,
) -> None:
    """Publish exactly once and remove the registry entry before new admission."""

    with _PROCESS_OBSERVER_FLIGHT_LOCK:
        flight.result = result
        if _PROCESS_OBSERVER_FLIGHTS.get(key) is flight:
            del _PROCESS_OBSERVER_FLIGHTS[key]
        flight.event.set()


def _process_observer_transport_singleflight(
    key: tuple[str, ...],
    dispatch: Callable[[], _ProcessObserverTransportResult],
    *,
    wait_seconds: float = _PROCESS_OBSERVER_FOLLOWER_WAIT_SECONDS,
    timeout_code: str = "broker-singleflight-timeout",
    invalid_code: str = "broker-singleflight-invalid",
    leader_exception_code: str = "broker-leader-exception",
) -> tuple[_ProcessObserverTransportResult, dict[str, object]]:
    """Share only one in-progress transport; completed results are never cached."""

    with _PROCESS_OBSERVER_FLIGHT_LOCK:
        flight = _PROCESS_OBSERVER_FLIGHTS.get(key)
        leader = flight is None
        if flight is None:
            flight = _ProcessObserverFlight()
            _PROCESS_OBSERVER_FLIGHTS[key] = flight

    if not leader:
        if not flight.event.wait(wait_seconds):
            return (
                _ProcessObserverTransportResult(timeout_code),
                {"singleflight_role": "follower", "broker_shared": True},
            )
        result = flight.result
        if not isinstance(result, _ProcessObserverTransportResult):
            result = _ProcessObserverTransportResult(invalid_code)
        return result, {"singleflight_role": "follower", "broker_shared": True}

    try:
        result = dispatch()
        if not isinstance(result, _ProcessObserverTransportResult):
            raise TypeError("process observer dispatch returned an invalid result")
    except BaseException:
        _publish_process_observer_flight(
            key,
            flight,
            _ProcessObserverTransportResult(leader_exception_code),
        )
        raise
    _publish_process_observer_flight(key, flight, result)
    return result, {"singleflight_role": "leader", "broker_shared": False}



def _transport_with_cross_process(
    result: _ProcessObserverTransportResult,
    *,
    role: str,
    shared: bool,
    cross_result: str,
) -> _ProcessObserverTransportResult:
    return _ProcessObserverTransportResult(
        result.result_code,
        bytes(result.stdout),
        role,
        shared,
        cross_result,
    )


def _canonical_process_cross_root(
    expected_uid: int,
    *,
    runtime_root: Path | None = None,
) -> Path:
    """Return a private runtime directory for cross-process observer flights."""

    if expected_uid != os.geteuid():
        raise ReleaseError("cross-process observer UID differs from the current user")

    def validate_root(root: Path) -> Path:
        try:
            metadata = root.lstat()
        except OSError as exc:
            raise ReleaseError("cross-process observer runtime is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseError("cross-process observer runtime is unsafe")
        return root

    def validate_runtime_base(runtime: Path) -> Path:
        if not runtime.is_absolute():
            raise ReleaseError("cross-process observer runtime base is not absolute")
        normalized = Path(os.path.abspath(os.fspath(runtime)))
        try:
            resolved = normalized.resolve(strict=True)
            metadata = normalized.lstat()
        except OSError as exc:
            raise ReleaseError("cross-process observer runtime base is unavailable") from exc
        if (
            resolved != normalized
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseError("cross-process observer runtime base is unsafe")
        return normalized

    def create_under_runtime(
        runtime: Path,
        *,
        allow_unavailable: bool,
    ) -> Path | None:
        base = validate_runtime_base(runtime)
        root = base / _PROCESS_CROSS_ROOT_NAME
        try:
            root.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            if (
                allow_unavailable
                and exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}
                and not _path_lexists(root)
            ):
                return None
            raise ReleaseError("cross-process observer runtime cannot be created") from exc
        return validate_root(root)

    if runtime_root is not None:
        root = Path(runtime_root).absolute()
        try:
            root.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            raise ReleaseError("cross-process observer runtime cannot be created") from exc
        return validate_root(root)

    configured_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if configured_runtime:
        configured = create_under_runtime(
            Path(configured_runtime),
            allow_unavailable=True,
        )
        if configured is not None:
            return configured

    canonical = create_under_runtime(
        Path("/run/user") / str(expected_uid),
        allow_unavailable=True,
    )
    if canonical is not None:
        return canonical

    temporary = Path("/tmp")
    try:
        temporary_metadata = temporary.lstat()
    except OSError as exc:
        raise ReleaseError("containment observer runtime is unavailable") from exc
    conventional_tmp = bool(
        temporary_metadata.st_mode & stat.S_ISVTX
        and temporary_metadata.st_mode & stat.S_IWOTH
    )
    private_tmp = bool(
        temporary_metadata.st_uid == expected_uid
        and stat.S_IMODE(temporary_metadata.st_mode) == 0o700
    )
    if (
        stat.S_ISLNK(temporary_metadata.st_mode)
        or not stat.S_ISDIR(temporary_metadata.st_mode)
        or not (conventional_tmp or private_tmp)
    ):
        raise ReleaseError("containment temporary root is unsafe")
    boot_id = _read_boot_id(Path("/proc"))
    fallback = temporary / (
        f"{_PROCESS_CROSS_ROOT_NAME}-{expected_uid}-{boot_id.replace('-', '')[:16]}"
    )
    try:
        fallback.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as exc:
        raise ReleaseError("containment observer runtime cannot be created") from exc
    return validate_root(fallback)


def _read_cross_file(
    path: Path,
    *,
    expected_uid: int,
    max_bytes: int,
) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseError("cross-process observer file cannot be inspected") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size < 0
        or before.st_size > max_bytes
    ):
        raise ReleaseError("cross-process observer file is unsafe or oversized")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("cross-process observer file cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != expected_uid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
        ):
            raise ReleaseError("cross-process observer file changed during open")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes or len(content) != opened.st_size:
            raise ReleaseError("cross-process observer file changed during read")
        return content
    finally:
        os.close(descriptor)


def _read_cross_json(
    path: Path,
    *,
    expected_uid: int,
    max_bytes: int,
) -> dict[str, object] | None:
    content = _read_cross_file(path, expected_uid=expected_uid, max_bytes=max_bytes)
    if content is None:
        return None
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("cross-process observer JSON is invalid") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != content:
        raise ReleaseError("cross-process observer JSON is not canonical")
    return payload


def _cross_file_contract(path: Path, kind: str) -> tuple[re.Pattern[str], int]:
    """Return the closed filename grammar and content bound for one kind."""

    if kind == _PROCESS_CROSS_FILE_STATE:
        pattern = _PROCESS_CROSS_STATE_NAME
        max_bytes = _PROCESS_CROSS_MAX_STATE_BYTES
    elif kind == _PROCESS_CROSS_FILE_RESULT:
        pattern = _PROCESS_CROSS_RESULT_NAME
        max_bytes = _PROCESS_CROSS_MAX_RESULT_BYTES
    else:
        raise ReleaseError("cross-process observer file kind is invalid")
    if pattern.fullmatch(path.name) is None:
        raise ReleaseError("cross-process observer file kind does not match its name")
    return pattern, max_bytes


def _cross_file_metadata_authorized(metadata: os.stat_result, *, expected_uid: int) -> bool:
    return bool(
        not stat.S_ISLNK(metadata.st_mode)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == expected_uid
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_size >= 0
    )


def _cross_file_identity_matches(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    """Bind one pathname/open descriptor to an unchanged direct file."""

    return bool(
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _unlink_cross_file(
    path: Path,
    *,
    expected_uid: int,
    kind: str,
) -> None:
    """Remove exactly one state/result file with kind-bounded I/O.

    Small files are read only through their exact protocol bound to prove stable
    length. Oversized malformed files are never consumed: metadata and pathname
    identity are sufficient authority for discarding an unusable protocol file.
    The mandatory closed ``kind`` prevents a state path from silently inheriting
    the much larger result-file read budget.
    """

    _pattern, max_bytes = _cross_file_contract(path, kind)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReleaseError("cross-process observer file cannot be inspected") from exc
    if not _cross_file_metadata_authorized(before, expected_uid=expected_uid):
        raise ReleaseError("cross-process observer file is unsafe for removal")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReleaseError("cross-process observer file cannot be opened for removal") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not _cross_file_metadata_authorized(opened, expected_uid=expected_uid)
            or not _cross_file_identity_matches(before, opened)
        ):
            raise ReleaseError("cross-process observer file changed during removal open")

        if opened.st_size <= max_bytes:
            remaining = int(opened.st_size)
            read_bytes = 0
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                read_bytes += len(chunk)
                remaining -= len(chunk)
            if read_bytes != opened.st_size:
                raise ReleaseError("cross-process observer file changed during removal read")

        after_open = os.fstat(descriptor)
        if (
            not _cross_file_metadata_authorized(after_open, expected_uid=expected_uid)
            or not _cross_file_identity_matches(opened, after_open)
        ):
            raise ReleaseError("cross-process observer file changed before removal")
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReleaseError(
                "cross-process observer file cannot be revalidated for removal"
            ) from exc
        if (
            not _cross_file_metadata_authorized(observed, expected_uid=expected_uid)
            or not _cross_file_identity_matches(after_open, observed)
        ):
            raise ReleaseError("cross-process observer file pathname changed before removal")
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReleaseError("cross-process observer file cannot be removed") from exc
    finally:
        os.close(descriptor)


@contextmanager
def _cross_process_lock(
    root: Path,
    *,
    expected_uid: int,
    wait_seconds: float | None = None,
) -> Iterator[Path]:
    """Acquire the one bounded registry lock for short protocol transitions.

    Older source revisions created one ``flight-<key>.lock`` inode per key.
    Those files are deliberately left inert for the rest of the boot: unlinking
    an inode that already has waiters can split the mutual-exclusion domain.
    New source creates and uses only ``registry.lock``.
    """

    lock_path = root / _PROCESS_CROSS_REGISTRY_LOCK_NAME
    timeout = _PROCESS_CROSS_LOCK_WAIT_SECONDS if wait_seconds is None else float(
        wait_seconds
    )
    if not 0.0 <= timeout <= _PROCESS_CROSS_LOCK_MAX_WAIT_SECONDS:
        raise ReleaseError("cross-process observer lock wait is invalid")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ReleaseError(
            "cross-process observer registry lock cannot be opened"
        ) from exc
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ReleaseError("cross-process observer registry lock is unsafe")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ReleaseError(
                        "cross-process observer registry lock failed"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReleaseError(
                        "cross-process observer registry lock timed out"
                    ) from exc
                time.sleep(min(_PROCESS_CROSS_LOCK_POLL_SECONDS, remaining))
        # Recheck the pathname after acquiring the descriptor. This rejects a
        # same-user pathname replacement between open and lock acquisition.
        try:
            observed = lock_path.lstat()
            locked = os.fstat(descriptor)
        except OSError as exc:
            raise ReleaseError(
                "cross-process observer registry lock cannot be revalidated"
            ) from exc
        if (
            observed.st_dev != locked.st_dev
            or observed.st_ino != locked.st_ino
            or observed.st_uid != expected_uid
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600
            or not stat.S_ISREG(observed.st_mode)
        ):
            raise ReleaseError(
                "cross-process observer registry lock changed during acquisition"
            )
        yield lock_path
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

def _cross_state_fields() -> frozenset[str]:
    return frozenset(
        {
            "schema",
            "key_sha256",
            "nonce",
            "result_name",
            "leader_pid",
            "leader_start_ticks",
            "boot_id",
            "started_monotonic_ns",
            "phase",
            "published_monotonic_ns",
        }
    )


def _validate_cross_state(
    payload: Mapping[str, object],
    *,
    key_sha256: str,
    current_boot_id: str,
    now_ns: int,
) -> str:
    if set(payload) != _cross_state_fields():
        raise ReleaseError("cross-process flight state has unexpected fields")
    nonce = payload.get("nonce")
    result_name = payload.get("result_name")
    leader_pid = payload.get("leader_pid")
    leader_start_ticks = payload.get("leader_start_ticks")
    started_ns = payload.get("started_monotonic_ns")
    phase = payload.get("phase")
    published_ns = payload.get("published_monotonic_ns")
    if (
        payload.get("schema") != _PROCESS_CROSS_STATE_SCHEMA
        or payload.get("key_sha256") != key_sha256
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or result_name != f"result-{key_sha256}-{nonce}.json"
        or not isinstance(leader_pid, int)
        or isinstance(leader_pid, bool)
        or leader_pid <= 0
        or not isinstance(leader_start_ticks, int)
        or isinstance(leader_start_ticks, bool)
        or leader_start_ticks < 0
        or payload.get("boot_id") != current_boot_id
        or not isinstance(started_ns, int)
        or isinstance(started_ns, bool)
        or started_ns < 0
        or phase not in {"running", "published"}
        or not isinstance(published_ns, int)
        or isinstance(published_ns, bool)
        or published_ns < 0
    ):
        raise ReleaseError("cross-process flight state is invalid")
    if phase == "running":
        if published_ns != 0:
            raise ReleaseError("running cross-process flight has publication time")
        age = now_ns - started_ns
        active = (
            0 <= age <= _PROCESS_CROSS_MAX_AGE_NS
            and _process_start_time(leader_pid) == leader_start_ticks
        )
        return "running" if active else "stale"
    publication_age = now_ns - published_ns
    flight_duration = published_ns - started_ns
    if (
        published_ns < started_ns
        or not 0 <= publication_age <= _PROCESS_CROSS_LATE_JOIN_TTL_NS
        or not 0 <= flight_duration <= _PROCESS_CROSS_MAX_AGE_NS
    ):
        return "stale"
    return "published"


def _cross_result_payload(
    admission: _ProcessCrossAdmission,
    result: _ProcessObserverTransportResult,
) -> dict[str, object]:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", result.result_code) is None:
        raise ReleaseError("cross-process observer result code is invalid")
    stdout = bytes(result.stdout)
    if len(stdout) > _PROCESS_OBSERVER_STDOUT_BYTES:
        raise ReleaseError("cross-process observer stdout is oversized")
    start_ticks = _process_start_time(os.getpid())
    if start_ticks is None:
        raise ReleaseError("cross-process observer publisher identity is unavailable")
    return {
        "schema": _PROCESS_CROSS_RESULT_SCHEMA,
        "key_sha256": admission.key_sha256,
        "nonce": admission.nonce,
        "result_code": result.result_code,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stdout_size": len(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "publisher_pid": os.getpid(),
        "publisher_start_ticks": start_ticks,
        "boot_id": admission.boot_id,
        "published_monotonic_ns": time.monotonic_ns(),
    }


def _read_cross_result_details(
    admission: _ProcessCrossAdmission,
    *,
    expected_uid: int,
) -> tuple[_ProcessObserverTransportResult, int] | None:
    payload = _read_cross_json(
        admission.result_path,
        expected_uid=expected_uid,
        max_bytes=_PROCESS_CROSS_MAX_RESULT_BYTES,
    )
    if payload is None:
        return None
    expected_fields = {
        "schema",
        "key_sha256",
        "nonce",
        "result_code",
        "stdout_base64",
        "stdout_size",
        "stdout_sha256",
        "publisher_pid",
        "publisher_start_ticks",
        "boot_id",
        "published_monotonic_ns",
    }
    result_code = payload.get("result_code")
    encoded = payload.get("stdout_base64")
    size = payload.get("stdout_size")
    published_ns = payload.get("published_monotonic_ns")
    if (
        set(payload) != expected_fields
        or payload.get("schema") != _PROCESS_CROSS_RESULT_SCHEMA
        or payload.get("key_sha256") != admission.key_sha256
        or payload.get("nonce") != admission.nonce
        or not isinstance(result_code, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", result_code) is None
        or not isinstance(encoded, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or size > _PROCESS_OBSERVER_STDOUT_BYTES
        or not isinstance(payload.get("stdout_sha256"), str)
        or payload.get("publisher_pid") != admission.leader_pid
        or payload.get("publisher_start_ticks") != admission.leader_start_ticks
        or payload.get("boot_id") != admission.boot_id
        or not isinstance(published_ns, int)
        or isinstance(published_ns, bool)
        or published_ns < 0
        or not (0 <= time.monotonic_ns() - published_ns <= _PROCESS_CROSS_RESULT_TTL_NS)
    ):
        raise ReleaseError("cross-process observer result receipt is invalid")
    try:
        stdout = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise ReleaseError("cross-process observer result encoding is invalid") from exc
    if len(stdout) != size or sha256_bytes(stdout) != payload.get("stdout_sha256"):
        raise ReleaseError("cross-process observer result digest is invalid")
    return _ProcessObserverTransportResult(result_code, stdout), published_ns


def _read_cross_result(
    admission: _ProcessCrossAdmission,
    *,
    expected_uid: int,
) -> _ProcessObserverTransportResult | None:
    details = _read_cross_result_details(admission, expected_uid=expected_uid)
    return None if details is None else details[0]


def _cleanup_cross_results(
    root: Path,
    *,
    expected_uid: int,
    current_boot_id: str,
) -> _ProcessCrossUsage:
    """Retire stale files and return bounded protocol-storage usage.

    This function runs while the one cross-process registry lock is held. Form
    exactly one bounded directory snapshot so unrelated directory pollution
    cannot turn the global critical section into unbounded work. The same
    snapshot supplies cleanup, count and aggregate-byte accounting.
    """

    entries: list[Path] = []
    try:
        for index, path in enumerate(root.iterdir()):
            if index >= _PROCESS_CROSS_MAX_DIRECTORY_ENTRIES:
                raise ReleaseError(
                    "cross-process observer runtime entry bound exceeded"
                )
            entries.append(path)
    except OSError as exc:
        raise ReleaseError("cross-process observer runtime cannot be listed") from exc
    entries.sort(key=lambda path: path.name)

    inspected = 0
    now_ns = time.monotonic_ns()
    for path in entries:
        result_match = _PROCESS_CROSS_RESULT_NAME.fullmatch(path.name)
        state_match = _PROCESS_CROSS_STATE_NAME.fullmatch(path.name)
        if result_match is None and state_match is None:
            continue
        inspected += 1
        if inspected > 2 * _PROCESS_CROSS_MAX_CLEANUP_ENTRIES:
            break
        if result_match is not None:
            try:
                payload = _read_cross_json(
                    path,
                    expected_uid=expected_uid,
                    max_bytes=_PROCESS_CROSS_MAX_RESULT_BYTES,
                )
            except ReleaseError:
                _unlink_cross_file(
                    path,
                    expected_uid=expected_uid,
                    kind=_PROCESS_CROSS_FILE_RESULT,
                )
                continue
            if payload is None:
                continue
            published = payload.get("published_monotonic_ns")
            stale = (
                payload.get("schema") != _PROCESS_CROSS_RESULT_SCHEMA
                or payload.get("boot_id") != current_boot_id
                or not isinstance(published, int)
                or isinstance(published, bool)
                or published < 0
                or now_ns - published > _PROCESS_CROSS_RESULT_TTL_NS
                or now_ns < published
            )
            if stale:
                _unlink_cross_file(
                    path,
                    expected_uid=expected_uid,
                    kind=_PROCESS_CROSS_FILE_RESULT,
                )
            continue
        key_sha256 = str(state_match.group(1))
        try:
            state = _read_cross_json(
                path,
                expected_uid=expected_uid,
                max_bytes=_PROCESS_CROSS_MAX_STATE_BYTES,
            )
            if state is None or _validate_cross_state(
                state,
                key_sha256=key_sha256,
                current_boot_id=current_boot_id,
                now_ns=now_ns,
            ) == "stale":
                _unlink_cross_file(
                    path,
                    expected_uid=expected_uid,
                    kind=_PROCESS_CROSS_FILE_STATE,
                )
        except ReleaseError:
            _unlink_cross_file(
                path,
                expected_uid=expected_uid,
                kind=_PROCESS_CROSS_FILE_STATE,
            )

    remaining_directory_entries = 0
    remaining_results = 0
    remaining_states = 0
    total_result_bytes = 0
    for path in entries:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReleaseError(
                "cross-process observer protocol capacity cannot be inspected"
            ) from exc
        remaining_directory_entries += 1
        is_result = _PROCESS_CROSS_RESULT_NAME.fullmatch(path.name) is not None
        is_state = _PROCESS_CROSS_STATE_NAME.fullmatch(path.name) is not None
        if not is_result and not is_state:
            continue
        max_bytes = (
            _PROCESS_CROSS_MAX_RESULT_BYTES
            if is_result
            else _PROCESS_CROSS_MAX_STATE_BYTES
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size < 0
            or metadata.st_size > max_bytes
        ):
            raise ReleaseError("cross-process observer protocol entry is unsafe")
        if is_result:
            remaining_results += 1
            total_result_bytes += int(metadata.st_size)
        else:
            remaining_states += 1

    usage = _ProcessCrossUsage(
        remaining_directory_entries,
        remaining_results,
        remaining_states,
        total_result_bytes,
    )
    if (
        usage.result_count >= _PROCESS_CROSS_MAX_CLEANUP_ENTRIES
        or usage.state_count >= _PROCESS_CROSS_MAX_CLEANUP_ENTRIES
        or usage.total_result_bytes >= _PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES
    ):
        raise ReleaseError("cross-process observer protocol capacity is exhausted")
    return usage

def _admit_cross_process_flight(
    key: tuple[str, ...],
    *,
    expected_uid: int,
    runtime_root: Path | None,
    request_started_monotonic_ns: int | None = None,
) -> _ProcessCrossAdmission:
    root = _canonical_process_cross_root(expected_uid, runtime_root=runtime_root)
    key_sha256 = sha256_bytes(canonical_json_bytes(key))
    current_boot_id = _read_boot_id(Path("/proc"))
    state_path = root / f"flight-{key_sha256}.json"
    lock_path = root / _PROCESS_CROSS_REGISTRY_LOCK_NAME
    request_observed_ns = time.monotonic_ns()
    if request_started_monotonic_ns is None:
        request_started_ns = request_observed_ns
    elif (
        not isinstance(request_started_monotonic_ns, int)
        or isinstance(request_started_monotonic_ns, bool)
    ):
        raise ReleaseError("cross-process observer request start is invalid")
    else:
        request_started_ns = request_started_monotonic_ns
    if not 0 <= request_observed_ns - request_started_ns <= _PROCESS_CROSS_MAX_AGE_NS:
        raise ReleaseError("cross-process observer request start is invalid")
    with _cross_process_lock(root, expected_uid=expected_uid):
        _cleanup_cross_results(
            root,
            expected_uid=expected_uid,
            current_boot_id=current_boot_id,
        )
        state = _read_cross_json(
            state_path,
            expected_uid=expected_uid,
            max_bytes=_PROCESS_CROSS_MAX_STATE_BYTES,
        )
        if state is not None:
            try:
                # Another process may publish this state while we wait for the
                # registry lock. A timestamp sampled before that wait can be
                # older than the newly read state's start time, yielding a
                # negative age and a duplicate leader. Validate only with a
                # fresh sample taken after the authoritative state read.
                state_observed_ns = time.monotonic_ns()
                state_kind = _validate_cross_state(
                    state,
                    key_sha256=key_sha256,
                    current_boot_id=current_boot_id,
                    now_ns=state_observed_ns,
                )
            except ReleaseError:
                _unlink_cross_file(
                    state_path,
                    expected_uid=expected_uid,
                    kind=_PROCESS_CROSS_FILE_STATE,
                )
            else:
                admission = _ProcessCrossAdmission(
                    "follower",
                    root,
                    key_sha256,
                    str(state["nonce"]),
                    root / str(state["result_name"]),
                    state_path,
                    lock_path,
                    current_boot_id,
                    int(state["leader_pid"]),
                    int(state["leader_start_ticks"]),
                )
                if state_kind == "running":
                    # Publication stages state and makes the final result visible
                    # under this same registry lock. A running flight therefore
                    # has one authoritative leader; every caller joins it rather
                    # than creating duplicate broker work.
                    return admission
                if state_kind == "published":
                    try:
                        details = _read_cross_result_details(
                            admission,
                            expected_uid=expected_uid,
                        )
                    except ReleaseError:
                        details = None
                    if details is not None:
                        _observed, published_ns = details
                        if request_started_ns <= published_ns:
                            return admission
                        # The new request is post-publication and must start a
                        # fresh flight, but the old result remains available to
                        # followers admitted before publication until result TTL.
                    else:
                        # Missing/tampered published results are retired so they
                        # cannot force indefinite local fallback.
                        try:
                            _unlink_cross_file(
                                admission.result_path,
                                expected_uid=expected_uid,
                                kind=_PROCESS_CROSS_FILE_RESULT,
                            )
                        except ReleaseError:
                            pass
                    _unlink_cross_file(
                        state_path,
                        expected_uid=expected_uid,
                        kind=_PROCESS_CROSS_FILE_STATE,
                    )
                else:
                    _unlink_cross_file(
                        state_path,
                        expected_uid=expected_uid,
                        kind=_PROCESS_CROSS_FILE_STATE,
                    )
        nonce = uuid.uuid4().hex
        result_path = root / f"result-{key_sha256}-{nonce}.json"
        if _path_lexists(result_path):
            raise ReleaseError("cross-process observer nonce collided")
        leader_start_ticks = _process_start_time(os.getpid())
        if leader_start_ticks is None:
            raise ReleaseError("cross-process observer leader identity is unavailable")
        leader_started_ns = time.monotonic_ns()
        if not 0 <= leader_started_ns - request_started_ns <= _PROCESS_CROSS_MAX_AGE_NS:
            raise ReleaseError("cross-process observer request start is invalid")
        state_payload = {
            "schema": _PROCESS_CROSS_STATE_SCHEMA,
            "key_sha256": key_sha256,
            "nonce": nonce,
            "result_name": result_path.name,
            "leader_pid": os.getpid(),
            "leader_start_ticks": leader_start_ticks,
            "boot_id": current_boot_id,
            "started_monotonic_ns": leader_started_ns,
            "phase": "running",
            "published_monotonic_ns": 0,
        }
        _atomic_write_bytes(
            state_path,
            canonical_json_bytes(state_payload),
            mode=0o600,
        )
        return _ProcessCrossAdmission(
            "leader",
            root,
            key_sha256,
            nonce,
            result_path,
            state_path,
            lock_path,
            current_boot_id,
            os.getpid(),
            leader_start_ticks,
        )


def _clear_matching_cross_state(
    admission: _ProcessCrossAdmission,
    *,
    expected_uid: int,
) -> None:
    with _cross_process_lock(
        admission.root,
        expected_uid=expected_uid,
    ):
        state = _read_cross_json(
            admission.state_path,
            expected_uid=expected_uid,
            max_bytes=_PROCESS_CROSS_MAX_STATE_BYTES,
        )
        if (
            state is not None
            and state.get("schema") == _PROCESS_CROSS_STATE_SCHEMA
            and state.get("key_sha256") == admission.key_sha256
            and state.get("nonce") == admission.nonce
        ):
            _unlink_cross_file(
                admission.state_path,
                expected_uid=expected_uid,
                kind=_PROCESS_CROSS_FILE_STATE,
            )


def _cross_state_matches_admission(
    state: Mapping[str, object],
    admission: _ProcessCrossAdmission,
) -> bool:
    return bool(
        state.get("schema") == _PROCESS_CROSS_STATE_SCHEMA
        and state.get("key_sha256") == admission.key_sha256
        and state.get("nonce") == admission.nonce
        and state.get("result_name") == admission.result_path.name
        and state.get("leader_pid") == admission.leader_pid
        and state.get("leader_start_ticks") == admission.leader_start_ticks
        and state.get("boot_id") == admission.boot_id
    )


def _validate_cross_publication_state(
    admission: _ProcessCrossAdmission,
    state: Mapping[str, object] | None,
) -> dict[str, object]:
    if admission.role != "leader" or admission.leader_pid != os.getpid():
        raise ReleaseError("cross-process observer publication has no leader authority")
    if state is None or not _cross_state_matches_admission(state, admission):
        raise ReleaseError("cross-process observer state changed before publication")
    if _validate_cross_state(
        state,
        key_sha256=admission.key_sha256,
        current_boot_id=admission.boot_id,
        now_ns=time.monotonic_ns(),
    ) != "running":
        raise ReleaseError("cross-process observer state is not running at publication")
    if state.get("phase") != "running" or state.get("published_monotonic_ns") != 0:
        raise ReleaseError("cross-process observer state changed before publication")
    return dict(state)


def _restore_cross_publication_state(
    admission: _ProcessCrossAdmission,
    *,
    expected_uid: int,
    original_state: Mapping[str, object],
    published_monotonic_ns: int,
) -> None:
    """Remove a failed result and restore only this transaction's state."""

    rollback_errors: list[str] = []
    if _path_lexists(admission.result_path):
        try:
            _unlink_cross_file(
                admission.result_path,
                expected_uid=expected_uid,
                kind=_PROCESS_CROSS_FILE_RESULT,
            )
        except Exception as exc:
            rollback_errors.append(f"result: {exc}")
    try:
        current = _read_cross_json(
            admission.state_path,
            expected_uid=expected_uid,
            max_bytes=_PROCESS_CROSS_MAX_STATE_BYTES,
        )
    except Exception as exc:
        current = None
        rollback_errors.append(f"state-read: {exc}")
    if current is not None:
        if current == dict(original_state):
            pass
        elif (
            _cross_state_matches_admission(current, admission)
            and current.get("phase") == "published"
            and current.get("published_monotonic_ns") == published_monotonic_ns
        ):
            try:
                _atomic_write_bytes(
                    admission.state_path,
                    canonical_json_bytes(dict(original_state)),
                    mode=0o600,
                )
            except Exception as exc:
                try:
                    _unlink_cross_file(
                        admission.state_path,
                        expected_uid=expected_uid,
                        kind=_PROCESS_CROSS_FILE_STATE,
                    )
                except Exception as unlink_exc:
                    rollback_errors.append(
                        f"state-restore: {exc}; state-remove: {unlink_exc}"
                    )
        else:
            rollback_errors.append("state changed outside the publication transaction")
    if rollback_errors:
        raise ReleaseError(
            "cross-process observer publication rollback was incomplete: "
            + "; ".join(rollback_errors)
        )


def _publish_cross_result(
    admission: _ProcessCrossAdmission,
    result: _ProcessObserverTransportResult,
    *,
    expected_uid: int,
) -> _ProcessObserverTransportResult:
    """Commit one capacity-bound result at a single final-path linearization point.

    Dispatch and result construction happen before this function. Under the
    registry lock, state is staged as published and the complete final result is
    then atomically made visible. Admissions cannot observe the intermediate
    state, while existing followers cannot consume bytes before the final path
    appears.
    """

    payload = _cross_result_payload(admission, result)
    payload_bytes = canonical_json_bytes(payload)
    candidate_size = len(payload_bytes)
    if candidate_size <= 0 or candidate_size > _PROCESS_CROSS_MAX_RESULT_BYTES:
        raise ReleaseError("cross-process observer result receipt is oversized")
    published_ns = int(payload["published_monotonic_ns"])

    with _cross_process_lock(admission.root, expected_uid=expected_uid):
        usage = _cleanup_cross_results(
            admission.root,
            expected_uid=expected_uid,
            current_boot_id=admission.boot_id,
        )
        if (
            usage.directory_entries + 1 > _PROCESS_CROSS_MAX_DIRECTORY_ENTRIES
            or usage.result_count + 1 >= _PROCESS_CROSS_MAX_CLEANUP_ENTRIES
            or usage.total_result_bytes + candidate_size
            >= _PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES
        ):
            raise ReleaseError("cross-process observer protocol capacity is exhausted")
        if _path_lexists(admission.result_path):
            raise ReleaseError("cross-process observer result already exists")
        original_state = _validate_cross_publication_state(
            admission,
            _read_cross_json(
                admission.state_path,
                expected_uid=expected_uid,
                max_bytes=_PROCESS_CROSS_MAX_STATE_BYTES,
            ),
        )
        updated = dict(original_state)
        updated["phase"] = "published"
        updated["published_monotonic_ns"] = published_ns
        try:
            _atomic_write_bytes(
                admission.state_path,
                canonical_json_bytes(updated),
                mode=0o600,
            )
            # Final-path appearance is the commit point. Existing followers poll
            # only this nonce-bound path; new admissions remain excluded by the
            # registry lock until both files have been verified.
            _atomic_write_bytes(
                admission.result_path,
                payload_bytes,
                mode=0o600,
            )
            details = _read_cross_result_details(
                admission,
                expected_uid=expected_uid,
            )
            if details is None:
                raise ReleaseError("cross-process observer result was not published")
            observed, observed_published_ns = details
            if observed_published_ns != published_ns:
                raise ReleaseError("cross-process observer publication time changed")
            return observed
        except Exception as exc:
            try:
                _restore_cross_publication_state(
                    admission,
                    expected_uid=expected_uid,
                    original_state=original_state,
                    published_monotonic_ns=published_ns,
                )
            except Exception as rollback_exc:
                raise ReleaseError(
                    f"cross-process observer publication failed: {exc}; {rollback_exc}"
                ) from exc
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError(
                f"cross-process observer publication failed and was rolled back: {exc}"
            ) from exc

def _wait_cross_result(
    admission: _ProcessCrossAdmission,
    *,
    expected_uid: int,
) -> _ProcessObserverTransportResult:
    deadline = time.monotonic() + _PROCESS_CROSS_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            observed = _read_cross_result(admission, expected_uid=expected_uid)
        except ReleaseError:
            return _ProcessObserverTransportResult("cross-process-result-invalid")
        if observed is not None:
            return observed
        time.sleep(0.01)
    return _ProcessObserverTransportResult("cross-process-follower-timeout")


def _process_observer_cross_process_singleflight(
    key: tuple[str, ...],
    *,
    expected_uid: int,
    dispatch: Callable[[], _ProcessObserverTransportResult],
    runtime_root: Path | None = None,
    request_started_monotonic_ns: int | None = None,
) -> _ProcessObserverTransportResult:
    """Share one in-progress broker transport across same-user processes."""

    try:
        admission = _admit_cross_process_flight(
            key,
            expected_uid=expected_uid,
            runtime_root=runtime_root,
            request_started_monotonic_ns=request_started_monotonic_ns,
        )
    except (OSError, ReleaseError, RuntimeError, ValueError):
        local = dispatch()
        return _transport_with_cross_process(
            local,
            role="unavailable-local",
            shared=False,
            cross_result="unavailable-local",
        )
    if admission.role == "follower":
        observed = _wait_cross_result(admission, expected_uid=expected_uid)
        return _transport_with_cross_process(
            observed,
            role="follower",
            shared=True,
            cross_result=(
                observed.result_code
                if observed.result_code
                in {"cross-process-follower-timeout", "cross-process-result-invalid"}
                else "shared"
            ),
        )
    try:
        local = dispatch()
        if not isinstance(local, _ProcessObserverTransportResult):
            raise TypeError("cross-process observer dispatch returned an invalid result")
    except BaseException:
        try:
            _publish_cross_result(
                admission,
                _ProcessObserverTransportResult("cross-process-leader-exception"),
                expected_uid=expected_uid,
            )
        except Exception:
            try:
                _clear_matching_cross_state(admission, expected_uid=expected_uid)
            except Exception:
                pass
        raise
    try:
        observed = _publish_cross_result(
            admission,
            local,
            expected_uid=expected_uid,
        )
    except (OSError, ReleaseError, RuntimeError, ValueError):
        try:
            _clear_matching_cross_state(admission, expected_uid=expected_uid)
        except Exception:
            pass
        return _transport_with_cross_process(
            _ProcessObserverTransportResult("cross-process-publication-failed"),
            role="leader",
            shared=False,
            cross_result="cross-process-publication-failed",
        )
    return _transport_with_cross_process(
        observed,
        role="leader",
        shared=False,
        cross_result="published",
    )



def _validate_process_observer_transport_result(
    transport: _ProcessObserverTransportResult,
    *,
    layout: ReleaseLayout,
    state: Mapping[str, object],
    expected_uid: int,
    direct: Mapping[str, object],
    source_module: Path,
) -> tuple[dict[str, object] | None, str]:
    """Validate raw broker bytes against one caller's fresh evidence."""

    if transport.result_code != "broker-output-ready":
        return None, transport.result_code
    try:
        payload = json.loads(transport.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, "broker-output-invalid"
    try:
        current = state.get("current_release_id")
        expected_state_sha256 = sha256_bytes(canonical_json_bytes(state))
        observed_state = load_managed_state(layout)
        if sha256_bytes(canonical_json_bytes(observed_state)) != expected_state_sha256:
            raise ReleaseError("managed state changed before caller validation")
        observed_activation = _verify_managed_activation(
            layout,
            observed_state,
            verify_release=True,
        )
        if observed_activation.get("current_release_id") != current:
            raise ReleaseError("managed activation changed before caller validation")
        status = _validate_process_observer_receipt(
            payload,
            layout=layout,
            state=state,
            expected_uid=expected_uid,
            direct=direct,
            source_module=source_module,
        )
    except (OSError, ReleaseError, RuntimeError, ValueError):
        return None, "broker-receipt-rejected"
    return status, "accepted"


def _scan_managed_gui_processes_via_user_manager(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    expected_uid: int,
    direct: Mapping[str, object],
    request_started_monotonic_ns: int | None = None,
) -> tuple[dict[str, object] | None, str, dict[str, object]]:
    """Return one caller-validated scan from a bounded shared transport."""

    no_flight = {
        "singleflight_role": "none",
        "broker_shared": False,
        "cross_process_role": "none",
        "cross_process_shared": False,
        "cross_process_result": "not-attempted",
    }
    try:
        source_module = Path(__file__).resolve(strict=True)
        key = _process_observer_flight_key(
            layout,
            state,
            expected_uid=expected_uid,
            direct=direct,
            source_module=source_module,
        )
    except (OSError, ReleaseError, RuntimeError, ValueError):
        return None, "broker-dispatch-failed", no_flight

    def dispatch_share_safe() -> _ProcessObserverTransportResult:
        raw = _dispatch_process_observer_transport(
            layout,
            state,
            expected_uid=expected_uid,
            source_module=source_module,
        )
        _status, code = _validate_process_observer_transport_result(
            raw,
            layout=layout,
            state=state,
            expected_uid=expected_uid,
            direct=direct,
            source_module=source_module,
        )
        if code != "accepted":
            return _ProcessObserverTransportResult(code)
        return raw

    transport, flight_metadata = _process_observer_transport_singleflight(
        key,
        lambda: _process_observer_cross_process_singleflight(
            key,
            expected_uid=expected_uid,
            dispatch=dispatch_share_safe,
            request_started_monotonic_ns=request_started_monotonic_ns,
        ),
    )
    flight_metadata = {
        **flight_metadata,
        "cross_process_role": transport.cross_process_role,
        "cross_process_shared": transport.cross_process_shared,
        "cross_process_result": transport.cross_process_result,
    }
    status, result_code = _validate_process_observer_transport_result(
        transport,
        layout=layout,
        state=state,
        expected_uid=expected_uid,
        direct=direct,
        source_module=source_module,
    )
    return status, result_code, flight_metadata

def _canonical_real_proc_root(proc_root: Path) -> bool:
    try:
        return proc_root.resolve(strict=True) == Path("/proc").resolve(strict=True)
    except OSError:
        return False



def _direct_process_scan_flight_key(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    proc_root: Path,
    expected_uid: int,
    source_module: Path,
) -> tuple[str, ...]:
    """Return the exact namespace/state key for one in-progress direct scan."""

    try:
        resolved_proc_root = proc_root.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved_proc_root = proc_root.absolute()
    return (
        "direct-process-scan",
        str(layout.root),
        str(layout.bin_dir),
        str(expected_uid),
        sha256_bytes(canonical_json_bytes(state)),
        str(resolved_proc_root),
        str(source_module),
    )


def _direct_scan_failure_status(
    state: Mapping[str, object],
    *,
    expected_uid: int,
    issue_code: str,
) -> dict[str, object]:
    """Return a schema-complete status that cannot authorize broker or mutation."""

    current = state.get("current_release_id")
    configured = isinstance(current, str) and bool(current)
    posture, activation = _process_posture(complete=False, gui_count=0)
    return {
        "schema": _MANAGED_GUI_PROCESS_SCHEMA,
        "managed_release_configured": configured,
        "expected_uid": expected_uid,
        "boot_id": "",
        "expected_identity": {
            "python_executable": "",
            "bootstrap_path": "",
            "argv_prefix_sha256": "",
        },
        "complete": False,
        "posture": posture,
        "gui_count": 0,
        "processes": [],
        "activation": activation,
        "scanned_pid_count": 0,
        "same_uid_pid_count": 0,
        "ignored_process_count": 0,
        "vanished_process_count": 0,
        "raced_process_count": 0,
        "issues": [{"code": issue_code, "count": 1}],
    }


def _scan_managed_gui_processes_singleflight(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    proc_root: Path,
    expected_uid: int,
) -> tuple[dict[str, object], dict[str, object]]:
    """Share only an in-progress direct scan and parse a fresh result per caller."""

    no_flight = {
        "direct_singleflight_role": "none",
        "direct_shared": False,
        "direct_scan_result": "direct-scan-key-invalid",
    }
    try:
        source_module = Path(__file__).resolve(strict=True)
        key = _direct_process_scan_flight_key(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=expected_uid,
            source_module=source_module,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return (
            _direct_scan_failure_status(
                state,
                expected_uid=expected_uid,
                issue_code="direct-scan-key-invalid",
            ),
            no_flight,
        )

    def dispatch() -> _ProcessObserverTransportResult:
        try:
            status = _scan_managed_gui_processes(
                layout,
                state,
                proc_root=proc_root,
                expected_uid=expected_uid,
            )
            payload = canonical_json_bytes(status)
        except Exception:
            return _ProcessObserverTransportResult("direct-scan-failed")
        return _ProcessObserverTransportResult("direct-scan-output-ready", payload)

    transport, shared_metadata = _process_observer_transport_singleflight(
        key,
        dispatch,
        wait_seconds=_PROCESS_DIRECT_SCAN_FOLLOWER_WAIT_SECONDS,
        timeout_code="direct-singleflight-timeout",
        invalid_code="direct-singleflight-invalid",
        leader_exception_code="direct-leader-exception",
    )
    metadata = {
        "direct_singleflight_role": shared_metadata["singleflight_role"],
        "direct_shared": shared_metadata["broker_shared"],
        "direct_scan_result": transport.result_code,
    }
    if transport.result_code != "direct-scan-output-ready":
        return (
            _direct_scan_failure_status(
                state,
                expected_uid=expected_uid,
                issue_code=transport.result_code,
            ),
            metadata,
        )
    try:
        payload = json.loads(transport.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        payload = None
    configured = isinstance(state.get("current_release_id"), str) and bool(
        state.get("current_release_id")
    )
    valid = (
        isinstance(payload, Mapping)
        and payload.get("schema") == _MANAGED_GUI_PROCESS_SCHEMA
        and payload.get("managed_release_configured") is configured
        and payload.get("expected_uid") == expected_uid
        and isinstance(payload.get("complete"), bool)
        and isinstance(payload.get("posture"), str)
        and isinstance(payload.get("gui_count"), int)
        and not isinstance(payload.get("gui_count"), bool)
        and isinstance(payload.get("processes"), list)
        and isinstance(payload.get("activation"), Mapping)
        and isinstance(payload.get("issues"), list)
        and "observer" not in payload
        and not _receipt_contains_private_process_data(payload)
    )
    if not valid:
        metadata["direct_scan_result"] = "direct-scan-output-invalid"
        return (
            _direct_scan_failure_status(
                state,
                expected_uid=expected_uid,
                issue_code="direct-scan-output-invalid",
            ),
            metadata,
        )
    metadata["direct_scan_result"] = "accepted"
    return dict(payload), metadata


def _observe_managed_gui_processes(
    layout: ReleaseLayout,
    state: Mapping[str, object],
    *,
    proc_root: Path,
    expected_uid: int,
    allow_user_manager_broker: bool,
) -> dict[str, object]:
    request_started_monotonic_ns = time.monotonic_ns()
    direct, direct_metadata = _scan_managed_gui_processes_singleflight(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=expected_uid,
    )
    direct_result = dict(direct)
    direct_result["observer"] = {
        "mode": "direct",
        "direct_complete": bool(direct.get("complete")),
        "broker_attempted": False,
        "broker_result": "not-needed" if direct.get("complete") else "not-applicable",
        "singleflight_role": "none",
        "broker_shared": False,
        **direct_metadata,
    }
    if direct_metadata.get("direct_scan_result") != "accepted":
        return direct_result
    if direct.get("complete"):
        return direct_result
    if (
        not allow_user_manager_broker
        or os.environ.get(_PROCESS_OBSERVER_ENV) == "1"
        or not _canonical_real_proc_root(proc_root)
    ):
        return direct_result
    broker, broker_result, flight_metadata = _scan_managed_gui_processes_via_user_manager(
        layout,
        state,
        expected_uid=expected_uid,
        direct=direct,
        request_started_monotonic_ns=request_started_monotonic_ns,
    )
    if broker is None:
        direct_result["observer"] = {
            "mode": "direct",
            "direct_complete": False,
            "direct_posture": direct.get("posture"),
            "direct_issues": direct.get("issues"),
            "broker_attempted": True,
            "broker_result": broker_result,
            **flight_metadata,
            **direct_metadata,
        }
        return direct_result
    result = dict(broker)
    result["observer"] = {
        "mode": "systemd-user-manager",
        "direct_complete": False,
        "direct_posture": direct.get("posture"),
        "direct_issues": direct.get("issues"),
        "broker_attempted": True,
        "broker_result": "accepted",
        "stable_samples": 2,
        **flight_metadata,
        **direct_metadata,
    }
    return result


def managed_gui_process_status(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    proc_root: str | Path = "/proc",
    expected_uid: int | None = None,
    allow_user_manager_broker: bool = True,
) -> dict[str, object]:
    """Return a redaction-safe, read-only inventory of managed T1000 GUIs."""

    layout = release_layout(root=root, bin_dir=bin_dir)
    try:
        state = load_managed_state(layout)
    except (OSError, ReleaseError):
        posture, activation = _process_posture(complete=False, gui_count=0)
        return {
            "schema": _MANAGED_GUI_PROCESS_SCHEMA,
            "managed_release_configured": False,
            "expected_uid": os.getuid() if expected_uid is None else int(expected_uid),
            "complete": False,
            "posture": posture,
            "gui_count": 0,
            "processes": [],
            "activation": activation,
            "scanned_pid_count": 0,
            "same_uid_pid_count": 0,
            "ignored_process_count": 0,
            "vanished_process_count": 0,
            "raced_process_count": 0,
            "issues": [{"code": "managed-state-invalid", "count": 1}],
            "observer": {
                "mode": "direct",
                "direct_complete": False,
                "broker_attempted": False,
                "broker_result": "not-applicable",
                "singleflight_role": "none",
                "broker_shared": False,
                "direct_singleflight_role": "none",
                "direct_shared": False,
                "direct_scan_result": "managed-state-invalid",
            },
        }
    return _observe_managed_gui_processes(
        layout,
        state,
        proc_root=Path(proc_root),
        expected_uid=os.getuid() if expected_uid is None else int(expected_uid),
        allow_user_manager_broker=allow_user_manager_broker,
    )


def managed_release_status(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    verify: bool = False,
    proc_root: str | Path = "/proc",
    expected_uid: int | None = None,
) -> dict[str, object]:
    layout = release_layout(root=root, bin_dir=bin_dir)
    try:
        state = load_managed_state(layout)
    except (OSError, ReleaseError) as exc:
        return {
            "ok": False,
            "action": "release-status",
            "root": str(layout.root),
            "error": str(exc),
            "runtime_processes": managed_gui_process_status(
                root=root,
                bin_dir=bin_dir,
                proc_root=proc_root,
                expected_uid=expected_uid,
            ),
        }
    payload: dict[str, object] = {
        "ok": True,
        "action": "release-status",
        "root": str(layout.root),
        "launcher": str(layout.launcher_path),
        "launcher_exists": layout.launcher_path.is_file(),
        "state": state,
        "verified": False,
    }
    # Runtime posture is deliberately orthogonal to artifact integrity. A
    # stopped or multiply launched GUI does not make immutable release files
    # corrupt; callers receive both facts and may apply a fail-closed activation
    # policy without shell substring matching.
    payload["runtime_processes"] = _observe_managed_gui_processes(
        layout,
        state,
        proc_root=Path(proc_root),
        expected_uid=os.getuid() if expected_uid is None else int(expected_uid),
        allow_user_manager_broker=True,
    )
    if state == _default_state():
        return payload
    try:
        activation = _verify_managed_activation(
            layout,
            state,
            verify_release=verify,
        )
        autostart = (
            _verify_production_autostart(layout, state)
            if _production_manages_autostart(layout, production=True)
            else None
        )
    except (OSError, ReleaseError) as exc:
        payload.update({"ok": False, "error": str(exc)})
    else:
        payload["activation"] = activation
        if autostart is not None:
            payload["autostart"] = autostart
        if "release" in activation:
            payload["release"] = activation["release"]
        payload["verified"] = verify
    return payload


def _process_start_time(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _recovery_uuid(command: str | None) -> str | None:
    match = _UUID.search(command or "")
    return match.group(1).lower() if match else None


_SESSION_CORE_COLUMNS = frozenset(
    {
        "id",
        "name",
        "tag",
        "command",
        "cwd",
        "tmux_name",
        "created_at",
        "last_opened_at",
        "status",
    }
)


def _read_session_records_read_only(database: Path) -> tuple[SessionRecord, ...]:
    """Read the durable registry without creating directories or migrating it.

    Release verification is a non-interference operation. Constructing a
    ``SessionStore`` would chmod/create the auth catalogue and run schema
    migrations, which can fail in a sealed/read-only verifier and would also
    make the observation itself mutate the state being compared. SQLite's
    read-only URI mode sees the live WAL snapshot while ``query_only`` and an
    explicit read transaction make the contract fail closed.
    """

    if database == Path(":memory:"):
        raise ReleaseError("semantic runtime snapshots require a persistent SQLite database")
    if not database.exists():
        return ()
    try:
        source = database.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError(f"could not resolve session registry: {database}: {exc}") from exc
    if not source.is_file() or source.is_symlink():
        raise ReleaseError(f"session registry is not a regular file: {source}")

    uri = source.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        raise ReleaseError(f"could not open session registry read-only: {source}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN")
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        if table is None:
            raise ReleaseError("session registry has no sessions table")
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        missing = sorted(_SESSION_CORE_COLUMNS - columns)
        if missing:
            raise ReleaseError(f"session registry is missing required columns: {missing}")
        rows = connection.execute(
            "SELECT * FROM sessions WHERE status != 'archived' ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise ReleaseError(f"could not read session registry without mutation: {source}: {exc}") from exc
    finally:
        connection.close()

    def value(row: sqlite3.Row, name: str, default: object = None) -> object:
        return row[name] if name in columns else default

    records: list[SessionRecord] = []
    for row in rows:
        records.append(
            SessionRecord(
                id=str(row["id"]),
                name=str(row["name"]),
                tag=str(row["tag"]),
                command=str(row["command"]),
                cwd=str(row["cwd"]),
                tmux_name=str(row["tmux_name"]),
                created_at=str(row["created_at"]),
                last_opened_at=str(row["last_opened_at"]),
                status=str(row["status"]),
                last_pid=value(row, "last_pid"),  # type: ignore[arg-type]
                window_id=str(value(row, "window_id", "main")),
                window_title=str(value(row, "window_title", "Main")),
                tab_index=int(value(row, "tab_index", 0)),
                visible=bool(value(row, "visible", 1)),
                pinned=bool(value(row, "pinned", 0)),
                pane_target=value(row, "pane_target"),  # type: ignore[arg-type]
                external=bool(value(row, "external", 0)),
                recovery_command=value(row, "recovery_command"),  # type: ignore[arg-type]
                recovery_cwd=value(row, "recovery_cwd"),  # type: ignore[arg-type]
                auth_scope_id=value(row, "auth_scope_id"),  # type: ignore[arg-type]
                codex_home=value(row, "codex_home"),  # type: ignore[arg-type]
                selected_account=value(row, "selected_account"),  # type: ignore[arg-type]
                pending_account=value(row, "pending_account"),  # type: ignore[arg-type]
                auth_restart_required=bool(value(row, "auth_restart_required", 0)),
                auth_changed_at=value(row, "auth_changed_at"),  # type: ignore[arg-type]
            )
        )
    return tuple(records)


def semantic_runtime_snapshot(
    *,
    db_path: str | Path | None = None,
    backend: TmuxBackend | None = None,
) -> dict[str, object]:
    """Capture persistent session identities while ignoring client reconnect PIDs."""

    database = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    tmux = backend or TmuxBackend()
    records = _read_session_records_read_only(database)
    live = tmux.session_exists_many(records) if tmux.available else {
        record.id: False for record in records
    }
    active = [record for record in records if live.get(record.id, False)]
    pids = tmux.pane_pids(active) if active else {}
    rows: list[dict[str, object]] = []
    live_count = 0
    for record in sorted(records, key=lambda item: item.id):
        is_live = bool(live.get(record.id, False))
        live_count += int(is_live)
        pid = pids.get(record.id) if is_live else None
        if is_live:
            try:
                target = tmux.pane_target(record)
            except (AttributeError, TmuxError):
                target = record.pane_target or record.tmux_name
        else:
            target = record.pane_target or record.tmux_name
        rows.append(
            {
                "session_id": record.id,
                "registry_status": record.status,
                "tmux_name": record.tmux_name,
                "tmux_live": is_live,
                "pane_target": target,
                "pane_pid": pid,
                "pane_start_time": _process_start_time(pid),
                "persisted_last_pid": record.last_pid,
                "persisted_last_pid_start_time": _process_start_time(record.last_pid),
                "recovery_uuid": _recovery_uuid(record.recovery_command),
                "recovery_cwd": record.recovery_cwd,
                "auth_scope_id": record.auth_scope_id,
            }
        )
    digest = sha256_bytes(canonical_json_bytes(rows))
    return {
        "schema": "t1000.semantic-runtime-snapshot.v1",
        "db_path": str(database),
        "backend_available": bool(tmux.available),
        "row_count": len(rows),
        "live_row_count": live_count,
        "sha256": digest,
        "rows": rows,
    }
