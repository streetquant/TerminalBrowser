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
import tomllib
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

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
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_ACTIVATION_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_ACTIVATION_PROBE_OUTPUT_BYTES = 6 * 1024 * 1024
_MAX_ACTIVATION_PROBE_STDERR_BYTES = 64 * 1024
_ACTIVATION_PROBE_SCHEMA = "t1000.activation-probe.v1"
_RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION_PREFIX = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:[A-Za-z0-9.+-].*)?$"
)
_FROZEN_RELEASE_MIN_VERSION = (0, 2, 13)
_CANONICAL_WHEEL_METADATA_MIN_VERSION = (0, 2, 14)
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
        Path(root).expanduser() if root is not None else default_managed_root()
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
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
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


def _fsync_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReleaseError(f"could not inspect directory before fsync: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseError(f"cannot fsync a symlink or non-directory: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(f"could not open directory for fsync: {path}: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ReleaseError(f"directory changed type before fsync: {path}")
        os.fsync(descriptor)
    except OSError as exc:
        raise ReleaseError(f"could not fsync directory: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_mode = path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise ReleaseError(f"atomic-write parent is not a real directory: {path.parent}")
    if _path_lexists(path):
        destination_mode = path.lstat().st_mode
        if stat.S_ISLNK(destination_mode) or not stat.S_ISREG(destination_mode):
            raise ReleaseError(f"atomic-write destination is not a regular file: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _capture_file(path: Path, *, label: str) -> _FileSnapshot:
    if not _path_lexists(path):
        return _FileSnapshot(False)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseError(f"{label} is not a regular file: {path}")
    return _FileSnapshot(
        True,
        path.read_bytes(),
        stat.S_IMODE(mode),
    )


def _restore_file(path: Path, snapshot: _FileSnapshot, *, label: str) -> None:
    if snapshot.exists:
        _atomic_write_bytes(path, snapshot.content, mode=snapshot.mode)
        return
    if not _path_lexists(path):
        return
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseError(f"cannot remove changed {label}: {path}")
    path.unlink()
    _fsync_directory(path.parent)


def _publish_files_transactionally(
    writes: Sequence[tuple[Path, bytes, int, str]],
    *,
    verify: Callable[[], None] | None = None,
) -> None:
    """Publish a small activation set and restore every preimage on failure."""

    snapshots = [
        (path, _capture_file(path, label=label), label)
        for path, _content, _mode, label in writes
    ]
    try:
        for path, content, mode, _label in writes:
            _atomic_write_bytes(path, content, mode=mode)
        if verify is not None:
            verify()
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, snapshot, label in reversed(snapshots):
            try:
                _restore_file(path, snapshot, label=label)
            except Exception as rollback_exc:
                rollback_errors.append(f"{label}: {rollback_exc}")
        if rollback_errors:
            raise ReleaseError(
                f"activation publication failed: {exc}; rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise ReleaseError(f"activation publication failed and was rolled back: {exc}") from exc


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
        for name in directories:
            child = directory / name
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode) or not stat.S_ISDIR(child_mode):
                raise ReleaseError(f"installed release contains an unsafe directory: {child}")
    return count


def _installed_site_files(site: Path) -> set[str]:
    """Return the exact regular-file set, rejecting links and special files."""

    mode = site.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseError("installed release site is not a real directory")
    root = site.resolve(strict=True)
    files: set[str] = set()
    for current, directories, names in os.walk(site, topdown=True, followlinks=False):
        directory = Path(current)
        try:
            directory.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReleaseError(f"installed release directory escaped site: {directory}") from exc
        for name in directories:
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
    return files


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


def verify_installed_tree(release_dir: str | Path) -> dict[str, object]:
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
    if manifest_path.read_bytes() != canonical_json_bytes(manifest):
        raise ReleaseError("installed release manifest is not canonical")
    build_info = manifest.get("build_info")
    if not isinstance(build_info, Mapping):
        raise ReleaseError("installed release manifest has no build-info mapping")
    frozen_directories = _release_requires_frozen_directories(build_info)
    installed_directories = _verify_release_directories(
        root,
        frozen=frozen_directories,
    )
    wheel = root / "artifacts" / str(manifest["wheel"])
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
    actual_files = _installed_site_files(site)
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
    return {
        "ok": True,
        "release_id": manifest["release_id"],
        "wheel_sha256": verified.sha256,
        "installed_members": len(verified.members),
        "installed_directories": installed_directories,
        "frozen_directories": frozen_directories,
    }


def _bootstrap_source(layout: ReleaseLayout, python: Path) -> bytes:
    """Return a self-contained launcher that verifies bytes before importing code."""

    root = str(layout.root.resolve())
    template = r'''#!/usr/bin/env __PYTHON_NAME__
from __future__ import annotations
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__ROOT_REPR__)
STATE = ROOT / "state.json"
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


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def record_hash(value):
    encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe RECORD path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeError(f"unsafe RECORD path: {name!r}")
    if str(pure) != name:
        raise RuntimeError(f"non-canonical RECORD path: {name!r}")
    return pure


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


def real_directory(path, label):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"{label} is not a real directory: {path}")
    return path.resolve(strict=True)


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


def verify_release_directories(root, *, frozen):
    resolved_root = root.resolve(strict=True)

    def walk_error(error):
        raise RuntimeError(f"could not inspect managed release directories: {error}")

    for current, directories, _names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory = Path(current)
        mode = directory.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"managed release contains an unsafe directory: {directory}")
        directory.resolve(strict=True).relative_to(resolved_root)
        if frozen and stat.S_IMODE(mode) != 0o555:
            raise RuntimeError(
                f"managed frozen directory is writable or has wrong mode: {directory}"
            )
        for name in directories:
            child = directory / name
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode) or not stat.S_ISDIR(child_mode):
                raise RuntimeError(f"managed release contains an unsafe directory: {child}")


try:
    regular_file(STATE, "managed state")
    raw_state = STATE.read_bytes()
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
    if digest_file(bootstrap) != state["bootstrap_sha256"]:
        raise RuntimeError("managed bootstrap SHA-256 mismatch")
    launcher = Path(state.get("launcher_path", ""))
    if not launcher.is_absolute():
        raise RuntimeError("managed launcher path is not absolute")
    regular_file(launcher, "managed launcher", executable=True)
    if digest_file(launcher) != state["launcher_sha256"]:
        raise RuntimeError("managed launcher SHA-256 mismatch")

    releases_root = real_directory(ROOT / "releases", "managed releases root")
    release_path = releases_root / release_id
    release = real_directory(release_path, "managed release")
    release.relative_to(releases_root)
    manifest_path = regular_file(
        release / "manifest.json", "managed manifest", immutable=True
    )
    manifest_bytes = manifest_path.read_bytes()
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
    frozen_directories = frozen_directories_required(build_info)
    verify_release_directories(release, frozen=frozen_directories)
    wheel_hash = manifest.get("wheel_sha256")
    if not isinstance(wheel_hash, str) or re.fullmatch(r"[0-9a-f]{64}", wheel_hash) is None:
        raise RuntimeError("managed wheel SHA-256 is invalid")
    expected_release = digest_bytes(
        canonical({"build_id": build_info.get("build_id"), "wheel_sha256": wheel_hash})
    )
    if expected_release != release_id:
        raise RuntimeError("managed release_id does not bind build and wheel")

    wheel_name = manifest.get("wheel")
    if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
        raise RuntimeError("managed wheel name is unsafe")
    wheel = regular_file(
        release / "artifacts" / wheel_name, "managed wheel", immutable=True
    )
    if wheel.stat().st_size != manifest.get("wheel_size") or digest_file(wheel) != wheel_hash:
        raise RuntimeError("managed wheel artifact failed size/SHA-256 verification")

    site = real_directory(release / "site", "managed release site")
    build_info_path = regular_file(
        site / "termdeck" / "_build_info.json",
        "bundled build info",
        immutable=True,
    )
    if json.loads(build_info_path.read_text(encoding="utf-8")) != build_info:
        raise RuntimeError("bundled build info does not match the manifest")
    records = sorted(site.glob("*.dist-info/RECORD"))
    if len(records) != 1:
        raise RuntimeError("managed site must contain exactly one wheel RECORD")
    record_path = regular_file(
        records[0], "managed wheel RECORD", immutable=True
    )
    record_name = record_path.relative_to(site).as_posix()
    rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))))
    expected_files = set()
    for row in rows:
        if len(row) != 3:
            raise RuntimeError("managed RECORD row is malformed")
        name, expected_hash, expected_size = row
        pure = safe_member(name)
        target = regular_file(
            site.joinpath(*pure.parts), "managed RECORD member", immutable=True
        )
        resolved = target.resolve(strict=True)
        resolved.relative_to(site)
        expected_files.add(name)
        content = target.read_bytes()
        if name == record_name:
            if expected_hash or expected_size:
                raise RuntimeError("managed RECORD self-row is not empty")
            continue
        if record_hash(content) != expected_hash or str(len(content)) != expected_size:
            raise RuntimeError(f"managed RECORD verification failed: {name}")
    actual_files = set()
    for current, directories, names in os.walk(site, topdown=True, followlinks=False):
        directory = Path(current)
        directory.resolve(strict=True).relative_to(site)
        for name in directories:
            child = directory / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise RuntimeError(f"managed site contains an unsafe directory: {child}")
        for name in names:
            child = regular_file(
                directory / name, "managed site member", immutable=True
            )
            child.resolve(strict=True).relative_to(site)
            actual_files.add(child.relative_to(site).as_posix())
    if actual_files != expected_files:
        raise RuntimeError("managed site file set differs from wheel RECORD")

    latest_state = regular_file(STATE, "managed state").read_bytes()
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
except Exception as exc:
    print(f"t1000 managed launcher refused an invalid release: {exc}", file=sys.stderr)
    raise SystemExit(78)

os.environ.pop("T1000_BOOTSTRAP_STATE_RETRY", None)
os.environ["T1000_MANAGED_ROOT"] = str(ROOT)
os.environ["T1000_MANAGED_BIN_DIR"] = str(launcher.parent)
os.environ["T1000_RELEASE_MANIFEST"] = str(manifest_path)
os.environ["T1000_RELEASE_STATE"] = str(STATE)
sys.path.insert(0, str(site))
from termdeck.build_info import release_status
status = release_status(verify_artifact=False, managed_root=ROOT)
if not status.get("ok") or status.get("release_id") != release_id:
    print("t1000 managed launcher rejected inconsistent bundled identity", file=sys.stderr)
    raise SystemExit(78)
from termdeck.cli import main
sys.argv[0] = "t1000"
raise SystemExit(main())
'''
    source = template.replace("__PYTHON_NAME__", python.name).replace(
        "__ROOT_REPR__", repr(root)
    )
    return source.encode("utf-8")


def _launcher_source(layout: ReleaseLayout, python: Path) -> bytes:
    command = f'exec {shlex.quote(str(python))} -I -B {shlex.quote(str(layout.bootstrap_path))} "$@"\n'
    return ("#!/bin/sh\nset -eu\n" + command).encode("utf-8")


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
    if not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseError("release id is unsafe")
    release_dir = layout.releases_dir / release_id
    result = verify_installed_tree(release_dir)
    if result["release_id"] != release_id:
        raise ReleaseError("installed directory and manifest release ids disagree")
    return result


def _remove_staging_tree(
    layout: ReleaseLayout,
    stage: Path,
    *,
    release_id: str,
) -> None:
    """Remove only this installer's unique staging path, thawing directories safely."""

    expected_prefix = f".staging-{release_id}-"
    if stage.parent != layout.releases_dir or not stage.name.startswith(expected_prefix):
        raise ReleaseError(f"refusing to remove an unowned staging path: {stage}")
    if not _path_lexists(stage):
        return
    mode = stage.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        stage.unlink()
        _fsync_directory(layout.releases_dir)
        return

    def walk_error(error: OSError) -> None:
        raise ReleaseError(f"could not inspect failed staging tree: {error}")

    for current, directories, _files in os.walk(
        stage,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        directory = Path(current)
        entry_mode = directory.lstat().st_mode
        if stat.S_ISLNK(entry_mode) or not stat.S_ISDIR(entry_mode):
            raise ReleaseError(f"failed staging tree contains an unsafe directory: {directory}")
        directory.chmod(0o700)
        for name in directories:
            child = directory / name
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode):
                continue
            if not stat.S_ISDIR(child_mode):
                raise ReleaseError(f"failed staging tree contains a non-directory: {child}")
            child.chmod(0o700)
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        raise ReleaseError(f"could not remove failed staging tree: {stage}: {exc}") from exc
    _fsync_directory(layout.releases_dir)


def _stage_release(
    layout: ReleaseLayout,
    verified: VerifiedWheel,
    manifest_path: Path,
) -> Path:
    assert verified.manifest is not None
    release_id = str(verified.manifest["release_id"])
    layout.releases_dir.mkdir(parents=True, exist_ok=True)
    final = layout.releases_dir / release_id
    if final.exists():
        _verify_release_dir(layout, release_id)
        return final
    stage = layout.releases_dir / f".staging-{release_id}-{os.getpid()}-{uuid.uuid4().hex}"
    wheel_content = _verified_wheel_bytes(verified)
    try:
        stage.mkdir(mode=0o755)
        site = stage / "site"
        _extract_verified_wheel(verified, site, content=wheel_content)
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
        if _release_requires_frozen_directories(build_info):
            _freeze_release_tree(stage)
        verify_installed_tree(stage)
        os.replace(stage, final)
        _fsync_directory(layout.releases_dir)
    except Exception:
        if _path_lexists(stage):
            _remove_staging_tree(layout, stage, release_id=release_id)
        raise
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
) -> dict[str, object]:
    layout = release_layout(root=root, bin_dir=bin_dir)
    manifest_source = Path(manifest_path).expanduser()
    verified = verify_wheel(wheel_path, manifest_path=manifest_source)
    assert verified.manifest is not None
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

        release_dir = _stage_release(layout, verified, manifest_source)
        _verify_staged_import(release_dir, interpreter)
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

        if _state_digest(layout.state_path) != state_digest:
            raise ReleaseError("managed state changed during install; compare-and-swap refused")

        writes: list[tuple[Path, bytes, int, str]] = []
        if repaired_bootstrap:
            writes.append(
                (layout.bootstrap_path, expected_bootstrap, 0o755, "managed bootstrap")
            )
        if state_needs_update:
            writes.append(
                (
                    layout.state_path,
                    canonical_json_bytes(new_state),
                    0o600,
                    "managed state",
                )
            )
        if repaired_launcher:
            writes.append(
                (layout.launcher_path, expected_launcher, 0o755, "t1000 launcher")
            )

        def verify_publication() -> None:
            observed_state = load_managed_state(layout)
            if observed_state != new_state:
                raise ReleaseError("published managed state differs from the transaction")
            _verify_managed_activation(
                layout,
                observed_state,
                verify_release=True,
            )

        _publish_files_transactionally(writes, verify=verify_publication)

        result = {
            "ok": True,
            "action": "release-install",
            "idempotent": idempotent,
            "repaired": repaired_bootstrap or repaired_launcher or state_needs_update,
            "release_id": release_id,
            "previous_release_id": new_state.get("previous_release_id"),
            "root": str(layout.root),
            "release_dir": str(release_dir),
            "launcher": str(layout.launcher_path),
            "state_generation": new_state["generation"],
            "live_processes_restarted": False,
        }
        return result


def rollback_release(
    *,
    root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    release_id: str | None = None,
    to_legacy: bool = False,
    lock_timeout: float = 15.0,
) -> dict[str, object]:
    layout = release_layout(root=root, bin_dir=bin_dir)
    if release_id is not None and to_legacy:
        raise ReleaseError("choose either a managed release or --legacy, not both")
    with release_lock(layout, timeout=lock_timeout):
        state = load_managed_state(layout)
        state_digest = _state_digest(layout.state_path)
        current = state.get("current_release_id")
        if current is None:
            raise ReleaseError("there is no managed current release to roll back")

        if to_legacy:
            legacy = state.get("legacy_launcher")
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
            if _state_digest(layout.state_path) != state_digest:
                raise ReleaseError("managed state changed during rollback")

            def verify_legacy_publication() -> None:
                observed = load_managed_state(layout)
                if observed != new_state:
                    raise ReleaseError("published legacy rollback state differs from transaction")
                _verify_managed_activation(layout, observed, verify_release=False)

            _publish_files_transactionally(
                [
                    (
                        layout.launcher_path,
                        backup.read_bytes(),
                        legacy_mode,
                        "t1000 launcher",
                    ),
                    (
                        layout.state_path,
                        canonical_json_bytes(new_state),
                        0o600,
                        "managed state",
                    ),
                ],
                verify=verify_legacy_publication,
            )
            return {
                "ok": True,
                "action": "release-rollback",
                "target": "legacy-launcher",
                "previous_release_id": current,
                "state_generation": new_state["generation"],
                "live_processes_restarted": False,
            }

        target = release_id or state.get("previous_release_id")
        if not isinstance(target, str) or not _RELEASE_ID.fullmatch(target):
            raise ReleaseError("no verified previous managed release is available")
        if target == current:
            raise ReleaseError("rollback target is already current")
        _verify_release_dir(layout, target)
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
        if _state_digest(layout.state_path) != state_digest:
            raise ReleaseError("managed state changed during rollback")

        def verify_managed_publication() -> None:
            observed = load_managed_state(layout)
            if observed != new_state:
                raise ReleaseError("published managed rollback state differs from transaction")
            _verify_managed_activation(
                layout,
                observed,
                verify_release=True,
                expected_activation=target_activation,
            )

        _publish_files_transactionally(
            [
                (
                    layout.bootstrap_path,
                    target_activation.bootstrap,
                    0o755,
                    "managed bootstrap",
                ),
                (
                    layout.state_path,
                    canonical_json_bytes(new_state),
                    0o600,
                    "managed state",
                ),
                (
                    layout.launcher_path,
                    target_activation.launcher,
                    0o755,
                    "t1000 launcher",
                ),
            ],
            verify=verify_managed_publication,
        )
        return {
            "ok": True,
            "action": "release-rollback",
            "release_id": target,
            "previous_release_id": current,
            "state_generation": new_state["generation"],
            "activation_origin": target_activation.origin,
            "live_processes_restarted": False,
        }


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
        return {
            "current_release_id": None,
            "launcher_sha256": launcher_hash,
            "legacy": True,
        }
    if not isinstance(current, str) or not _RELEASE_ID.fullmatch(current):
        raise ReleaseError("managed current release id is invalid")

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

    manifest_path = layout.releases_dir / current / "manifest.json"
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
    return {
        "current_release_id": current,
        "python_executable": str(resolved_interpreter),
        "bootstrap_sha256": bootstrap_hash,
        "launcher_sha256": launcher_hash,
        "activation_origin": activation.origin,
        "release": release,
        "legacy": False,
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
        home = Path(account.pw_dir).resolve(strict=True)
        runtime = Path("/run/user") / str(expected_uid)
        runtime_metadata = runtime.lstat()
        bus = runtime / "bus"
        bus_metadata = bus.lstat()
    except (KeyError, OSError) as exc:
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
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": account.pw_shell or "/bin/sh",
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
    except (OSError, ReleaseError) as exc:
        payload.update({"ok": False, "error": str(exc)})
    else:
        payload["activation"] = activation
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
