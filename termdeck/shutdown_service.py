from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

USER_UNIT_NAME = "termdeck-checkpoint.service"
SYSTEM_UNIT_NAME = "termdeck-checkpoint.service"
DEFAULT_USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
DEFAULT_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
SYSTEM_UNIT_MODE = 0o644
_SAFE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SYSTEM_UNIT_MAX_BYTES = 256 * 1024
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_ATTRIB = 0x00000004
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ISDIR = 0x40000000
_INOTIFY_EVENT_HEADER_SIZE = 16

# The published wrappers are deliberately a single-account contract.  In
# particular, the stop wrapper uses ``runuser max``; allowing a caller to
# render ``user@other-uid.service`` would make systemd's ordering describe a
# different manager than the wrapper actually checkpoints.
CHECKPOINT_SERVICE_USER = "max"
CHECKPOINT_SERVICE_UID = 1000
CHECKPOINT_SERVICE_HOME = Path("/home/arch")

_LIBC: ctypes.CDLL | None = None
_RENAMEAT2 = None
_INOTIFY_INIT1 = None
_INOTIFY_ADD_WATCH = None
try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
except OSError:
    pass

if _LIBC is not None:
    try:
        _RENAMEAT2 = _LIBC.renameat2
        _RENAMEAT2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        _RENAMEAT2.restype = ctypes.c_int
    except AttributeError:  # pragma: no cover - Linux provides renameat2.
        _RENAMEAT2 = None
    try:
        _INOTIFY_INIT1 = _LIBC.inotify_init1
        _INOTIFY_INIT1.argtypes = (ctypes.c_int,)
        _INOTIFY_INIT1.restype = ctypes.c_int
        _INOTIFY_ADD_WATCH = _LIBC.inotify_add_watch
        _INOTIFY_ADD_WATCH.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        _INOTIFY_ADD_WATCH.restype = ctypes.c_int
    except AttributeError:  # pragma: no cover - Linux provides inotify.
        _INOTIFY_INIT1 = None
        _INOTIFY_ADD_WATCH = None

# The deployed services all use the same three physical roots.  Keep these
# values in one place so a generated checkpoint unit cannot silently drift from
# the six static service units or from the managed wrapper publication.
TERMDECK_SOURCE_ROOT = Path("/warp/projects/softwares/termdeck")
TERMDECK_RUNTIME_ROOT = TERMDECK_SOURCE_ROOT / ".termdeck"
TERMDECK_STATE_ROOT = TERMDECK_RUNTIME_ROOT / "state"
TERMDECK_MANAGED_ROOT = TERMDECK_RUNTIME_ROOT / "t1000-managed"
CHECKPOINT_START_WRAPPER = TERMDECK_MANAGED_ROOT / "termdeck-checkpoint"
CHECKPOINT_STOP_WRAPPER = TERMDECK_MANAGED_ROOT / "termdeck-checkpoint-stop"
USER_CHECKPOINT_REGISTRATION_ERROR = (
    "user checkpoint service cannot be registered: the managed checkpoint stop "
    "wrapper requires root to invoke /usr/bin/runuser; install the system "
    "checkpoint service with --system"
)
REQUIRED_MOUNT_ROOTS = (
    TERMDECK_SOURCE_ROOT,
    TERMDECK_RUNTIME_ROOT,
    TERMDECK_STATE_ROOT,
)
# Public aliases used by deployment checks and callers that prefer the
# terminology used in the systemd unit contract.
CANONICAL_MOUNT_ROOTS = REQUIRED_MOUNT_ROOTS
CANONICAL_SOURCE_ROOT = TERMDECK_SOURCE_ROOT
CANONICAL_RUNTIME_ROOT = TERMDECK_RUNTIME_ROOT
CANONICAL_STATE_ROOT = TERMDECK_STATE_ROOT
CANONICAL_MANAGED_ROOT = TERMDECK_MANAGED_ROOT


class ShutdownServiceError(RuntimeError):
    pass


class _SystemUnitDirectoryMissing(ShutdownServiceError):
    """An anchored unit directory was absent before an idempotent removal."""


@dataclass(frozen=True)
class _SystemDirectoryIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class _SystemFileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _SystemLeafIdentity:
    """Identity for an untrusted leaf that must be restored, not opened."""

    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _SystemUnitSnapshot:
    content: bytes
    identity: _SystemFileIdentity | None

    @property
    def exists(self) -> bool:
        return self.identity is not None


@dataclass(frozen=True)
class _SystemUnitWriteReceipt:
    """Identity-bound publication result used before each systemctl call."""

    identity: _SystemFileIdentity
    cleanup_status: str


@dataclass
class _SystemDirectoryAnchor:
    path: Path
    descriptor: int
    chain: tuple[_SystemDirectoryIdentity, ...]
    label: str

    def assert_current(self, *, phase: str) -> None:
        if self.descriptor < 0:
            raise ShutdownServiceError(
                f"system unit parent descriptor is unavailable {phase}: {self.path}"
            )
        try:
            current = _system_directory_identity(
                os.fstat(self.descriptor),
                label=self.label,
            )
        except OSError as exc:
            raise ShutdownServiceError(
                f"system unit parent descriptor is unavailable {phase}: {self.path}: {exc}"
            ) from exc
        if current != self.chain[-1]:
            raise ShutdownServiceError(
                f"system unit parent identity changed {phase}: {self.path}"
            )
        fresh = _open_system_unit_directory(
            self.path,
            label=self.label,
            create_parents=False,
        )
        try:
            if fresh.chain != self.chain:
                raise ShutdownServiceError(
                    f"system unit parent identity changed {phase}: {self.path}"
                )
        finally:
            fresh.close()

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
class _SystemStageDirectory:
    parent: _SystemDirectoryAnchor
    name: str
    descriptor: int
    identity: _SystemDirectoryIdentity
    # A shutdown transaction deliberately never unlinks a private stage.  The
    # status is carried into the public receipt so callers can distinguish a
    # durably retained record from a race or fsync failure that left its exact
    # retained name uncertain.
    cleanup_status: str = "active"
    retention_attempted: bool = False

    def assert_current(self, *, phase: str) -> None:
        self.parent.assert_current(phase=phase)
        if self.descriptor < 0:
            raise ShutdownServiceError(f"system unit stage directory is unavailable {phase}")
        try:
            current = _system_directory_identity(
                os.fstat(self.descriptor),
                label="system unit stage directory",
            )
        except OSError as exc:
            raise ShutdownServiceError(
                f"system unit stage directory is unavailable {phase}: {exc}"
            ) from exc
        if current != self.identity:
            raise ShutdownServiceError(f"system unit stage directory changed {phase}")
        observed = _lstat_system_child(
            self.parent.descriptor,
            self.name,
            label="system unit stage directory",
        )
        if _system_directory_identity(
            observed,
            label="system unit stage directory",
        ) != self.identity:
            raise ShutdownServiceError(f"system unit stage directory changed {phase}")

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
class _StagedSystemUnit:
    stage: _SystemStageDirectory
    name: str
    descriptor: int
    identity: _SystemFileIdentity
    named: bool = True

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            os.close(descriptor)
        except OSError:
            pass


@dataclass(frozen=True)
class ShutdownServiceResult:
    """Registration receipt; detail also records private-stage retention."""

    action: str
    path: Path
    enabled: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "path": str(self.path),
            "enabled": self.enabled,
            "detail": self.detail,
        }


def _resolve_t1000_executable(executable: str | Path | None = None) -> Path:
    if executable is not None:
        candidate = Path(executable).expanduser()
    else:
        discovered = shutil.which("t1000")
        candidate = Path(discovered) if discovered else Path(sys.argv[0])
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ShutdownServiceError(f"TermDeck executable not found: {candidate}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ShutdownServiceError(f"TermDeck executable is not runnable: {candidate}")
    if "\n" in str(candidate) or "\r" in str(candidate):
        raise ShutdownServiceError("TermDeck executable path contains a newline")
    return candidate


def _systemd_quote(value: str | Path) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_required_mounts(
    required_mounts: tuple[str | Path, ...],
) -> None:
    """Reject a unit whose mount dependency set drifts from deployment."""

    if not required_mounts:
        return
    supplied = tuple(Path(path).expanduser() for path in required_mounts)
    if supplied != REQUIRED_MOUNT_ROOTS:
        expected = " ".join(str(path) for path in REQUIRED_MOUNT_ROOTS)
        actual = " ".join(str(path) for path in supplied)
        raise ShutdownServiceError(
            f"required mounts must be exactly {expected}; got {actual}"
        )


def _mounts_directive() -> str:
    return "RequiresMountsFor=" + " ".join(str(path) for path in REQUIRED_MOUNT_ROOTS)


def _rendered_unit_text(value: str | Path) -> str:
    """Read a unit for verification, or accept already-rendered text."""

    if isinstance(value, Path):
        try:
            return value.read_text(encoding="utf-8")
        except OSError as exc:
            raise ShutdownServiceError(f"could not read checkpoint unit: {value}") from exc
    return value


def _render_user_checkpoint_service() -> str:
    return f"""[Unit]
Description=Persist TermDeck sessions before user-manager shutdown
Documentation=https://github.com/Rebel0789/t1000
After=warp.mount
Before=exit.target
ConditionPathIsMountPoint=/warp
{_mounts_directive()}

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=HOME={TERMDECK_STATE_ROOT}
ExecStart={CHECKPOINT_START_WRAPPER}
ExecStop={CHECKPOINT_STOP_WRAPPER}
TimeoutStopSec=15min
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def render_user_checkpoint_service(executable: str | Path | None = None) -> str:
    """Render the legacy user-manager checkpoint unit text.

    ``executable`` remains accepted for CLI/API compatibility with older
    callers.  The service command is intentionally fixed to the managed
    checkpoint wrappers so an arbitrary checkout executable cannot become a
    long-lived shutdown hook.  The managed stop wrapper invokes ``runuser``;
    therefore :func:`install_user_checkpoint_service` refuses to register this
    text from a user manager.
    """

    del executable
    return _render_user_checkpoint_service()


def render_system_checkpoint_service(
    executable: str | Path | None,
    *,
    user: str,
    uid: int,
    home: str | Path,
    required_mounts: tuple[str | Path, ...] = (),
) -> str:
    """Render a system unit ordered before the complete user manager stops."""

    rendered_uid = _validate_checkpoint_service_identity(user=user, uid=uid, home=home)
    _validate_required_mounts(required_mounts)
    del executable
    return f"""[Unit]
Description=Persist TermDeck sessions before the user manager stops
Documentation=https://github.com/Rebel0789/t1000
After=warp.mount user@{rendered_uid}.service local-fs.target
Before=umount.target shutdown.target
ConditionPathIsMountPoint=/warp
ConditionPathIsDirectory={TERMDECK_STATE_ROOT}
ConditionPathIsDirectory={TERMDECK_MANAGED_ROOT}
{_mounts_directive()}

[Service]
Type=oneshot
RemainAfterExit=yes
KillMode=control-group
Environment=HOME={TERMDECK_STATE_ROOT}
ExecStart={CHECKPOINT_START_WRAPPER}
ExecStop={CHECKPOINT_STOP_WRAPPER}
Restart=on-failure
RestartMode=direct
TimeoutStopSec=15min
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _validate_checkpoint_service_identity(
    *,
    user: str,
    uid: int,
    home: str | Path,
) -> int:
    """Keep systemd ordering tied to the account used by the stop wrapper."""

    try:
        home_path = Path(home)
    except TypeError as exc:
        raise ShutdownServiceError(
            "checkpoint system service identity must be exactly "
            "user='max', uid=1000, home='/home/arch'"
        ) from exc
    if (
        type(user) is not str
        or not _SAFE_USER.fullmatch(user)
        or user != CHECKPOINT_SERVICE_USER
        or type(uid) is not int
        or uid != CHECKPOINT_SERVICE_UID
        or not home_path.is_absolute()
        or home_path != CHECKPOINT_SERVICE_HOME
    ):
        raise ShutdownServiceError(
            "checkpoint system service identity must be exactly "
            "user='max', uid=1000, home='/home/arch'"
        )
    return uid


def _render_system_checkpoint_service_bytes() -> bytes:
    """Return the one root-managed unit eligible for system removal."""

    return render_system_checkpoint_service(
        None,
        user=CHECKPOINT_SERVICE_USER,
        uid=CHECKPOINT_SERVICE_UID,
        home=CHECKPOINT_SERVICE_HOME,
        required_mounts=CANONICAL_MOUNT_ROOTS,
    ).encode("utf-8")


def verify_user_checkpoint_service(unit: str | Path) -> bool:
    """Verify a user checkpoint unit against the exact managed contract."""

    actual = _rendered_unit_text(unit)
    expected = _render_user_checkpoint_service()
    if actual != expected:
        raise ShutdownServiceError("user checkpoint unit does not match managed contract")
    return True


def verify_system_checkpoint_service(
    unit: str | Path,
    *,
    user: str,
    uid: int,
    home: str | Path,
    required_mounts: tuple[str | Path, ...] = (),
) -> bool:
    """Verify a system checkpoint unit against the exact managed contract."""

    actual = _rendered_unit_text(unit)
    expected = render_system_checkpoint_service(
        None,
        user=user,
        uid=uid,
        home=home,
        required_mounts=required_mounts,
    )
    if actual != expected:
        raise ShutdownServiceError("system checkpoint unit does not match managed contract")
    return True


def verify_checkpoint_service(
    unit: str | Path,
    *,
    system: bool = False,
    user: str | None = None,
    uid: int | None = None,
    home: str | Path | None = None,
    required_mounts: tuple[str | Path, ...] = (),
) -> bool:
    """Verify either supported checkpoint unit form.

    System units require the same identity arguments used during rendering;
    user units have no caller-owned identity fields.
    """

    if not system:
        return verify_user_checkpoint_service(unit)
    if user is None or uid is None or home is None:
        raise ShutdownServiceError("system checkpoint verification requires user, uid, and home")
    return verify_system_checkpoint_service(
        unit,
        user=user,
        uid=uid,
        home=home,
        required_mounts=required_mounts,
    )


def _run_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShutdownServiceError(f"systemctl --user failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "systemctl --user failed").strip()
        raise ShutdownServiceError(detail)
    return completed


def _run_systemctl_system(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["systemctl", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShutdownServiceError(f"systemctl failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "systemctl failed").strip()
        raise ShutdownServiceError(detail)
    return completed


def install_user_checkpoint_service(
    *,
    executable: str | Path | None = None,
    unit_dir: str | Path | None = None,
    enable: bool = True,
) -> ShutdownServiceResult:
    """Refuse an unsafe user-manager registration.

    The managed checkpoint stop wrapper uses ``/usr/bin/runuser`` to enter the
    service account.  A user manager cannot invoke that root-only transition,
    so writing or enabling its unit would defer a deterministic failure until
    shutdown.  Keep the public function so older callers receive a clear,
    fail-closed error rather than an apparently successful registration.
    """

    del executable, unit_dir, enable
    raise ShutdownServiceError(USER_CHECKPOINT_REGISTRATION_ERROR)


def uninstall_user_checkpoint_service(
    *,
    unit_dir: str | Path | None = None,
    disable: bool = True,
) -> ShutdownServiceResult:
    directory = _system_unit_directory_path(unit_dir or DEFAULT_USER_UNIT_DIR)
    path = directory / USER_UNIT_NAME
    owner = (os.geteuid(), os.getegid())
    parent = _open_checkpoint_unit_parent_for_removal(
        directory,
        owner=owner,
        label="user checkpoint unit directory",
    )
    cleanup_status = "not-needed"
    present = False
    disable_error: ShutdownServiceError | None = None
    try:
        expected_content = _render_user_checkpoint_service().encode("utf-8")
        snapshot = (
            _snapshot_checkpoint_unit_for_removal(
                parent,
                owner=owner,
                label="user checkpoint unit",
                expected_content=expected_content,
            )
            if parent is not None
            else _SystemUnitSnapshot(b"", None)
        )
        present = snapshot.exists
        if disable:
            if parent is not None:
                _assert_checkpoint_unit_removal_snapshot_current(
                    parent,
                    snapshot,
                    owner=owner,
                    label="user checkpoint unit",
                    expected_content=expected_content,
                    phase="immediately before systemctl --user disable",
                )
            try:
                _run_systemctl(["disable", "--now", USER_UNIT_NAME])
            except ShutdownServiceError as exc:
                # Removing an already absent/inactive unit remains idempotent,
                # but a live manager failure must remain visible when the
                # anchored target we inspected still exists.
                if present:
                    raise
                disable_error = exc
        if parent is not None:
            _removed, cleanup_status = _remove_checkpoint_unit_anchored(
                parent,
                owner=owner,
                label="user checkpoint unit",
                expected_content=expected_content,
                snapshot=snapshot,
            )
            if present:
                _assert_checkpoint_unit_absent(
                    parent,
                    label="user checkpoint unit",
                    phase="after private removal retention",
                )
            else:
                _assert_checkpoint_unit_removal_snapshot_current(
                    parent,
                    snapshot,
                    owner=owner,
                    label="user checkpoint unit",
                    expected_content=expected_content,
                    phase="after systemctl --user disable",
                )
    finally:
        if parent is not None:
            parent.close()
    if disable and disable_error is None:
        _run_systemctl(["daemon-reload"])
    if disable_error is not None:
        detail = (
            "shutdown checkpoint unit was already absent; "
            f"systemctl --user disable was not completed: {disable_error}"
        )
    elif disable:
        detail = "shutdown checkpoint service disabled"
    else:
        detail = "shutdown checkpoint unit removal requested"
    if cleanup_status != "not-needed":
        detail = f"{detail}; {_private_cleanup_receipt(cleanup_status)}"
    return ShutdownServiceResult("uninstall", path, False, detail)


def _system_unit_target_owner() -> tuple[int, int]:
    """Return the ownership required for a system-manager unit.

    The installer has already checked that its effective uid is root.  Keeping
    this in a small helper gives tests a way to exercise the descriptor-based
    transaction in an unprivileged temporary directory without weakening the
    production ownership contract.
    """

    return 0, 0


def _system_unit_directory_path(value: str | Path) -> Path:
    """Make a lexical absolute path without resolving a possible symlink."""

    raw = os.fspath(Path(value).expanduser())
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ShutdownServiceError("unsafe system unit directory path")
    return Path(os.path.normpath(os.path.abspath(raw)))


def _canonical_system_unit_directory() -> Path:
    """Return the only directory whose fixed unit name systemctl may manage."""

    return _system_unit_directory_path(DEFAULT_SYSTEM_UNIT_DIR)


def _require_canonical_systemctl_directory(directory: Path, *, action: str) -> None:
    """Keep a fixed-name global systemctl call bound to its unit file path."""

    canonical = _canonical_system_unit_directory()
    if directory != canonical:
        raise ShutdownServiceError(
            f"systemctl {action} requires the canonical system unit directory: {canonical}"
        )


def _assert_installed_system_unit_current(
    directory: Path,
    *,
    content: bytes,
    identity: _SystemFileIdentity,
    owner: tuple[int, int],
    verify_readback: Callable[[bytes], None],
    phase: str,
) -> None:
    """Re-read the exact published leaf before or after global systemctl.

    A fixed unit name is meaningful to systemctl only in the canonical system
    unit directory.  Keep the registration call bound to the same full leaf
    identity that the descriptor-anchored writer published.
    """

    parent = _open_system_unit_directory(
        directory,
        label="system unit directory",
        create_parents=False,
    )
    try:
        _validate_system_unit_parent(parent, owner=owner)
        parent.assert_current(phase=phase)
        observed = _read_system_unit_child(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            label="installed system checkpoint unit",
            expected=identity,
            owner=owner,
        )
        if observed is None:
            raise ShutdownServiceError(f"installed system checkpoint unit disappeared {phase}")
        actual, current = observed
        if current != identity or current.mode != SYSTEM_UNIT_MODE:
            raise ShutdownServiceError(f"installed system checkpoint unit identity changed {phase}")
        if actual != content:
            raise ShutdownServiceError(f"installed system checkpoint unit content changed {phase}")
        try:
            verify_readback(actual)
        except UnicodeDecodeError as exc:
            raise ShutdownServiceError("installed system checkpoint unit is not valid UTF-8") from exc
        parent.assert_current(phase=phase)
    finally:
        parent.close()


def _system_directory_identity(
    status: os.stat_result,
    *,
    label: str,
) -> _SystemDirectoryIdentity:
    if not stat.S_ISDIR(status.st_mode):
        raise ShutdownServiceError(f"{label} is not a directory")
    return _SystemDirectoryIdentity(
        device=status.st_dev,
        inode=status.st_ino,
        mode=stat.S_IMODE(status.st_mode),
        uid=status.st_uid,
        gid=status.st_gid,
    )


def _system_file_identity(
    status: os.stat_result,
    *,
    label: str,
) -> _SystemFileIdentity:
    if not stat.S_ISREG(status.st_mode):
        raise ShutdownServiceError(f"{label} is not a regular file")
    if status.st_nlink != 1:
        raise ShutdownServiceError(f"{label} must not have multiple hard links")
    return _SystemFileIdentity(
        device=status.st_dev,
        inode=status.st_ino,
        mode=stat.S_IMODE(status.st_mode),
        uid=status.st_uid,
        gid=status.st_gid,
        nlink=status.st_nlink,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
        ctime_ns=status.st_ctime_ns,
    )


def _same_system_file_after_rename(
    observed: _SystemFileIdentity,
    reference: _SystemFileIdentity,
) -> bool:
    """Compare object and durable metadata while allowing rename's ctime bump."""

    return (
        observed.device,
        observed.inode,
        observed.mode,
        observed.uid,
        observed.gid,
        observed.nlink,
        observed.size,
        observed.mtime_ns,
    ) == (
        reference.device,
        reference.inode,
        reference.mode,
        reference.uid,
        reference.gid,
        reference.nlink,
        reference.size,
        reference.mtime_ns,
    )


def _system_leaf_identity(status: os.stat_result) -> _SystemLeafIdentity:
    return _SystemLeafIdentity(
        device=status.st_dev,
        inode=status.st_ino,
        mode=status.st_mode,
        uid=status.st_uid,
        gid=status.st_gid,
        nlink=status.st_nlink,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
    )


def _refresh_staged_system_unit_identity(staged: _StagedSystemUnit) -> None:
    try:
        staged.identity = _system_file_identity(
            os.fstat(staged.descriptor),
            label="staged system unit",
        )
    except OSError as exc:
        raise ShutdownServiceError(f"could not inspect staged system unit: {exc}") from exc


def _fsync_system_descriptor(descriptor: int, *, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ShutdownServiceError(f"could not fsync {label}: {exc}") from exc


def _set_system_descriptor_owner(
    descriptor: int,
    owner: tuple[int, int],
    *,
    label: str,
) -> None:
    try:
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != owner:
            os.fchown(descriptor, *owner)
        final = os.fstat(descriptor)
    except OSError as exc:
        raise ShutdownServiceError(f"could not set {label} owner: {exc}") from exc
    if (final.st_uid, final.st_gid) != owner:
        raise ShutdownServiceError(f"{label} owner does not match system identity")


def _set_system_descriptor_mode(
    descriptor: int,
    mode: int,
    *,
    label: str,
) -> None:
    try:
        os.fchmod(descriptor, mode)
        current = os.fstat(descriptor)
    except OSError as exc:
        raise ShutdownServiceError(f"could not set {label} mode: {exc}") from exc
    if stat.S_IMODE(current.st_mode) != mode:
        raise ShutdownServiceError(f"{label} mode does not match managed contract")


def _lstat_system_child(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ShutdownServiceError(f"{label} disappeared") from exc
    except OSError as exc:
        raise ShutdownServiceError(f"could not inspect {label}: {exc}") from exc


def _untrusted_system_leaf_identity(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> _SystemLeafIdentity | None:
    try:
        status = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShutdownServiceError(f"could not inspect {label}: {exc}") from exc
    return _system_leaf_identity(status)


def _system_nofollow_open_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if isinstance(flag, bool) or not isinstance(flag, int) or flag == 0:
        raise ShutdownServiceError(
            "system unit installation requires O_NOFOLLOW for safe publication"
        )
    return flag


def _system_nonblock_open_flag() -> int:
    flag = getattr(os, "O_NONBLOCK", 0)
    if isinstance(flag, bool) or not isinstance(flag, int) or flag == 0:
        raise ShutdownServiceError(
            "system unit installation requires O_NONBLOCK to reject FIFO leaves"
        )
    return flag


def _system_directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    if isinstance(directory, bool) or not isinstance(directory, int) or directory == 0:
        raise ShutdownServiceError(
            "system unit installation requires O_DIRECTORY for safe publication"
        )
    return (
        os.O_RDONLY
        | directory
        | _system_nofollow_open_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )


@dataclass
class _SystemCreationFence:
    """An inotify proof that a just-created private directory was not swapped.

    ``mkdirat`` does not return a descriptor.  A first ``stat`` after it is
    therefore insufficient: a writer can replace the name before that stat
    and make a foreign directory look like the creation result.  The fence is
    armed on the parent *descriptor* before mkdir, then requires precisely the
    expected CREATE event and no name-level transition through open/fstat.
    """

    descriptor: int
    watch_descriptor: int
    name: bytes
    label: str
    create_events: int = 0

    def assert_trusted(self, *, phase: str) -> None:
        forbidden = _IN_MOVED_FROM | _IN_MOVED_TO | _IN_DELETE | _IN_ATTRIB
        parent_forbidden = _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT | _IN_IGNORED
        while True:
            try:
                payload = os.read(self.descriptor, 65536)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as exc:
                raise ShutdownServiceError(
                    f"could not read {self.label} creation fence {phase}: {exc}"
                ) from exc
            if not payload:
                raise ShutdownServiceError(
                    f"{self.label} creation fence closed unexpectedly {phase}"
                )
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < _INOTIFY_EVENT_HEADER_SIZE:
                    raise ShutdownServiceError(
                        f"{self.label} creation fence returned a truncated event {phase}"
                    )
                watch_descriptor = int.from_bytes(
                    payload[offset : offset + 4],
                    byteorder=sys.byteorder,
                    signed=True,
                )
                mask = int.from_bytes(
                    payload[offset + 4 : offset + 8],
                    byteorder=sys.byteorder,
                    signed=False,
                )
                name_length = int.from_bytes(
                    payload[offset + 12 : offset + 16],
                    byteorder=sys.byteorder,
                    signed=False,
                )
                event_end = offset + _INOTIFY_EVENT_HEADER_SIZE + name_length
                if event_end > len(payload):
                    raise ShutdownServiceError(
                        f"{self.label} creation fence returned an invalid event {phase}"
                    )
                event_name = payload[
                    offset + _INOTIFY_EVENT_HEADER_SIZE : event_end
                ].split(b"\0", 1)[0]
                offset = event_end

                if mask & _IN_Q_OVERFLOW:
                    raise ShutdownServiceError(
                        f"{self.label} creation fence overflowed {phase}"
                    )
                if watch_descriptor != self.watch_descriptor:
                    continue
                if mask & parent_forbidden:
                    raise ShutdownServiceError(
                        f"{self.label} parent changed during creation {phase}"
                    )
                if event_name != self.name:
                    continue
                if mask & forbidden:
                    raise ShutdownServiceError(
                        f"{self.label} creation fence observed a name transition {phase}"
                    )
                if mask & _IN_CREATE:
                    if not mask & _IN_ISDIR:
                        raise ShutdownServiceError(
                            f"{self.label} creation fence observed a non-directory create {phase}"
                        )
                    self.create_events += 1

        if self.create_events != 1:
            raise ShutdownServiceError(
                f"{self.label} creation fence did not observe exactly one directory create {phase}"
            )

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            os.close(descriptor)
        except OSError:
            pass


def _arm_system_creation_fence(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> _SystemCreationFence:
    """Watch the exact parent FD before a mkdir that will later be mutated."""

    if _INOTIFY_INIT1 is None or _INOTIFY_ADD_WATCH is None:
        raise ShutdownServiceError(
            "system unit directory creation requires inotify to fence mkdirat races"
        )
    flags = _system_nonblock_open_flag() | getattr(os, "O_CLOEXEC", 0)
    ctypes.set_errno(0)
    descriptor = _INOTIFY_INIT1(flags)
    if descriptor < 0:
        error = ctypes.get_errno()
        message = os.strerror(error) if error else "unknown error"
        raise ShutdownServiceError(f"could not arm {label} creation fence: {message}")
    try:
        watch_path = f"/proc/self/fd/{parent_descriptor}".encode("ascii")
        mask = (
            _IN_CREATE
            | _IN_MOVED_FROM
            | _IN_MOVED_TO
            | _IN_DELETE
            | _IN_ATTRIB
            | _IN_DELETE_SELF
            | _IN_MOVE_SELF
            | _IN_UNMOUNT
        )
        ctypes.set_errno(0)
        watch_descriptor = _INOTIFY_ADD_WATCH(descriptor, watch_path, mask)
        if watch_descriptor < 0:
            error = ctypes.get_errno()
            message = os.strerror(error) if error else "unknown error"
            raise ShutdownServiceError(
                f"could not arm {label} creation fence: {message}"
            )
        return _SystemCreationFence(
            descriptor,
            watch_descriptor,
            os.fsencode(name),
            label,
        )
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _open_system_unit_directory(
    path: Path,
    *,
    label: str,
    create_parents: bool,
    owner: tuple[int, int] | None = None,
) -> _SystemDirectoryAnchor:
    """Open ``path`` one nofollow component at a time.

    The returned descriptor remains attached to the directory that was
    validated.  Re-opening the lexical path in ``assert_current`` detects a
    later ancestor replacement before any name in that directory is mutated.
    """

    path = _system_unit_directory_path(path)
    flags = _system_directory_open_flags()
    current_descriptor = -1
    try:
        try:
            current_descriptor = os.open("/", flags)
        except OSError as exc:
            raise ShutdownServiceError(f"could not open filesystem root: {exc}") from exc
        chain: list[_SystemDirectoryIdentity] = [
            _system_directory_identity(os.fstat(current_descriptor), label="filesystem root")
        ]
        components = tuple(component for component in str(path).split(os.sep) if component)
        prefix = Path("/")
        for component in components:
            created = False
            creation_fence: _SystemCreationFence | None = None
            child_descriptor = -1
            try:
                try:
                    named_status = os.stat(
                        component,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not create_parents:
                        raise _SystemUnitDirectoryMissing(
                            f"{label} does not exist: {path}"
                        ) from None
                    if _system_directory_identity(
                        os.fstat(current_descriptor), label=f"{label} parent"
                    ) != chain[-1]:
                        raise ShutdownServiceError(
                            f"{label} parent changed while creating {path}"
                        )
                    _assert_system_directory_prefix_current(
                        prefix,
                        tuple(chain),
                        label=label,
                    )
                    # mkdirat does not return the new inode.  Arm an event
                    # fence on this already-open parent *before* creation so
                    # a replacement before the first lstat cannot be passed
                    # off as our private component.
                    creation_fence = _arm_system_creation_fence(
                        current_descriptor,
                        component,
                        label=f"{label} component {component!r}",
                    )
                    try:
                        # Keep a newly named lexical component private until
                        # the creation fence and descriptor identity checks
                        # have made it safe to publish its normal directory
                        # mode below.
                        os.mkdir(component, 0o700, dir_fd=current_descriptor)
                    except FileExistsError as exc:
                        # We observed this component absent before arming the
                        # fence.  EEXIST therefore proves another writer won
                        # the name race; its CREATE event cannot establish
                        # that the resulting directory is ours.  Do not
                        # collapse this into the ordinary pre-existing path,
                        # which could otherwise let a same-owner foreign
                        # directory receive the checkpoint unit.
                        raise ShutdownServiceError(
                            f"{label} component appeared after it was observed absent: "
                            f"{component!r}"
                        ) from exc
                    except OSError as exc:
                        creation_fence.close()
                        creation_fence = None
                        raise ShutdownServiceError(
                            f"could not create {label} component {component!r}: {exc}"
                        ) from exc
                    else:
                        created = True
                    if created:
                        _system_unit_creation_probe(
                            "directory-component-after-mkdir-before-first-stat:"
                            f"{component}"
                        )
                    try:
                        named_status = os.stat(
                            component,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise ShutdownServiceError(
                            f"could not inspect {label} component {component!r}: {exc}"
                        ) from exc
                except OSError as exc:
                    raise ShutdownServiceError(
                        f"could not inspect {label} component {component!r}: {exc}"
                    ) from exc

                named_identity = _system_directory_identity(
                    named_status,
                    label=f"{label} component {component!r}",
                )
                try:
                    child_descriptor = os.open(
                        component,
                        flags,
                        dir_fd=current_descriptor,
                    )
                except OSError as exc:
                    raise ShutdownServiceError(
                        f"could not open {label} component {component!r} without following links: {exc}"
                    ) from exc
                child_identity = _system_directory_identity(
                    os.fstat(child_descriptor),
                    label=f"{label} component {component!r}",
                )
                # Check the lexical parent again while we still only hold the
                # child descriptor.  A replacement of an ancestor must fail
                # before the descriptor-bound ownership or mode mutations.
                if created:
                    _assert_system_directory_prefix_current(
                        prefix,
                        tuple(chain),
                        label=label,
                    )
                    observed_identity = _system_directory_identity(
                        _lstat_system_child(
                            current_descriptor,
                            component,
                            label=f"{label} component {component!r}",
                        ),
                        label=f"{label} component {component!r}",
                    )
                    if creation_fence is None:  # pragma: no cover - invariant above
                        raise ShutdownServiceError(
                            f"{label} component creation fence is unavailable: {component!r}"
                        )
                    creation_fence.assert_trusted(
                        phase=f"through opening component {component!r}"
                    )
                    creation_fence.close()
                    creation_fence = None
                    if (
                        child_identity != named_identity
                        or observed_identity != named_identity
                    ):
                        raise ShutdownServiceError(
                            f"{label} component changed while opening: {component!r}"
                        )
                elif child_identity != named_identity:
                    raise ShutdownServiceError(
                        f"{label} component changed while opening: {component!r}"
                    )
                if created:
                    if owner is not None:
                        _set_system_descriptor_owner(
                            child_descriptor,
                            owner,
                            label=f"{label} component {component!r}",
                        )
                    _set_system_descriptor_mode(
                        child_descriptor,
                        0o755,
                        label=f"{label} component {component!r}",
                    )
                    child_identity = _system_directory_identity(
                        os.fstat(child_descriptor),
                        label=f"{label} component {component!r}",
                    )
                    _fsync_system_descriptor(
                        child_descriptor,
                        label=f"{label} component {component!r}",
                    )
                    _fsync_system_descriptor(
                        current_descriptor,
                        label=f"{label} parent while creating {component!r}",
                    )
                    # The fence is intentionally closed before descriptor
                    # metadata changes (which generate ATTRIB).  Re-check the
                    # name after every mutation and durability barrier; if a
                    # foreign component appeared, we have only touched the
                    # detached descriptor and fail without changing it.
                    if _system_directory_identity(
                        _lstat_system_child(
                            current_descriptor,
                            component,
                            label=f"{label} component {component!r}",
                        ),
                        label=f"{label} component {component!r}",
                    ) != child_identity:
                        raise ShutdownServiceError(
                            f"{label} component changed while initializing: {component!r}"
                        )
            except BaseException:
                if child_descriptor >= 0:
                    try:
                        os.close(child_descriptor)
                    except OSError:
                        pass
                raise
            finally:
                if creation_fence is not None:
                    creation_fence.close()
            try:
                os.close(current_descriptor)
            except OSError as exc:
                try:
                    os.close(child_descriptor)
                except OSError:
                    pass
                raise ShutdownServiceError(
                    f"could not close {label} parent after opening {component!r}: {exc}"
                ) from exc
            current_descriptor = child_descriptor
            chain.append(child_identity)
            prefix = prefix / component
        return _SystemDirectoryAnchor(path, current_descriptor, tuple(chain), label)
    except BaseException:
        if current_descriptor >= 0:
            try:
                os.close(current_descriptor)
            except OSError:
                pass
        raise


def _assert_system_directory_prefix_current(
    path: Path,
    expected: tuple[_SystemDirectoryIdentity, ...],
    *,
    label: str,
) -> None:
    fresh = _open_system_unit_directory(
        path,
        label=label,
        create_parents=False,
    )
    try:
        if fresh.chain != expected:
            raise ShutdownServiceError(
                f"{label} parent identity changed before directory creation: {path}"
            )
    finally:
        fresh.close()


def _validate_system_unit_parent(
    parent: _SystemDirectoryAnchor,
    *,
    owner: tuple[int, int],
) -> None:
    identity = parent.chain[-1]
    if (identity.uid, identity.gid) != owner:
        raise ShutdownServiceError("system unit directory owner does not match system identity")
    if identity.mode & 0o022:
        raise ShutdownServiceError("system unit directory must not be group or world writable")


def _fsync_system_unit_parent(
    parent: _SystemDirectoryAnchor,
    *,
    phase: str,
) -> None:
    parent.assert_current(phase=f"before {phase} directory fsync")
    _fsync_system_descriptor(parent.descriptor, label=f"system unit directory ({phase})")
    parent.assert_current(phase=f"after {phase} directory fsync")


def _fsync_system_stage_directory(
    stage: _SystemStageDirectory,
    *,
    phase: str,
) -> None:
    stage.assert_current(phase=f"before {phase} directory fsync")
    _fsync_system_descriptor(stage.descriptor, label=f"system unit stage directory ({phase})")
    stage.assert_current(phase=f"after {phase} directory fsync")


def _create_system_stage_directory(
    parent: _SystemDirectoryAnchor,
    *,
    owner: tuple[int, int],
) -> _SystemStageDirectory:
    parent.assert_current(phase="before creating stage directory")
    flags = _system_directory_open_flags()
    for _attempt in range(32):
        name = f".{SYSTEM_UNIT_NAME}.install-{os.getpid()}-{uuid.uuid4().hex}"
        # Every retry gets a freshly armed watch.  Events from a prior failed
        # name must never be used as proof for this private directory.
        parent.assert_current(phase="before reserving stage directory")
        creation_fence = _arm_system_creation_fence(
            parent.descriptor,
            name,
            label="system unit stage directory",
        )
        try:
            os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            creation_fence.close()
            continue
        except OSError as exc:
            creation_fence.close()
            raise ShutdownServiceError(f"could not create system unit stage directory: {exc}") from exc
        descriptor = -1
        stage: _SystemStageDirectory | None = None
        created_identity: _SystemDirectoryIdentity | None = None
        retention_identity: _SystemDirectoryIdentity | None = None
        try:
            # ``mkdirat`` has no descriptor-returning form.  The watch was
            # armed before it, so this first lstat is useful only after the
            # fence proves no writer moved, deleted, or changed this name
            # through descriptor acquisition.
            _system_unit_creation_probe("stage-after-mkdir-before-first-stat")
            created_identity = _system_directory_identity(
                _lstat_system_child(
                    parent.descriptor,
                    name,
                    label="new system unit stage directory",
                ),
                label="new system unit stage directory",
            )
            parent.assert_current(phase="after creating stage directory")
            _system_unit_stage_probe("after-stage-mkdir-preimage")
            parent.assert_current(phase="before opening created stage directory")
            try:
                descriptor = os.open(name, flags, dir_fd=parent.descriptor)
            except OSError as exc:
                # An open failure means the directory cannot be accepted or
                # mutated, but it may still be safe to retain the private
                # creation record.  Prove the name stayed put through the
                # failed open attempt before allowing the no-replace retention
                # path to use its identity; otherwise leave the ambiguous
                # name untouched.
                try:
                    failed_open_identity = _system_directory_identity(
                        _lstat_system_child(
                            parent.descriptor,
                            name,
                            label="new system unit stage directory after failed open",
                        ),
                        label="new system unit stage directory after failed open",
                    )
                    creation_fence.assert_trusted(
                        phase="through failed stage directory open"
                    )
                except ShutdownServiceError:
                    pass
                else:
                    if failed_open_identity == created_identity:
                        retention_identity = created_identity
                raise ShutdownServiceError(
                    f"could not open created system unit stage directory: {exc}"
                ) from exc
            opened_identity = _system_directory_identity(
                os.fstat(descriptor),
                label="system unit stage directory",
            )
            named_identity = _system_directory_identity(
                _lstat_system_child(
                    parent.descriptor,
                    name,
                    label="system unit stage directory",
                ),
                label="system unit stage directory",
            )
            parent.assert_current(phase="after opening created stage directory")
            creation_fence.assert_trusted(
                phase="through opening and checking stage directory"
            )
            creation_fence.close()
            if opened_identity != created_identity or named_identity != created_identity:
                raise ShutdownServiceError(
                    "created system unit stage directory changed while opening"
                )
            # This is the first point at which the name is proven to be the
            # private directory created above.  Do not retain or mutate a
            # pre-first-lstat replacement if the fence rejected it.
            retention_identity = created_identity
            # These changes are descriptor-bound.  A later name swap cannot
            # alter the foreign directory substituted at the old pathname.
            _set_system_descriptor_owner(
                descriptor,
                owner,
                label="system unit stage directory",
            )
            _set_system_descriptor_mode(
                descriptor,
                0o700,
                label="system unit stage directory",
            )
            identity = _system_directory_identity(
                os.fstat(descriptor),
                label="system unit stage directory",
            )
            retention_identity = identity
            named_identity = _system_directory_identity(
                _lstat_system_child(
                    parent.descriptor,
                    name,
                    label="system unit stage directory",
                ),
                label="system unit stage directory",
            )
            if named_identity != identity:
                raise ShutdownServiceError(
                    "created system unit stage directory changed while initializing"
                )
            stage = _SystemStageDirectory(parent, name, descriptor, identity)
            descriptor = -1
            _fsync_system_stage_directory(stage, phase="stage-create")
            _fsync_system_unit_parent(parent, phase="stage-create")
            return stage
        except BaseException as exc:
            creation_fence.close()
            cleanup_status = "not-needed"
            if stage is not None:
                cleanup_status = _retain_system_stage_best_effort(stage)
                stage.close()
            elif retention_identity is not None:
                cleanup_status = _retain_created_system_stage_best_effort(
                    parent,
                    name,
                    retention_identity,
                )
            else:
                # The mkdir succeeded but its identity could not be captured.
                # Leave the unique private name in place rather than guessing
                # at a pathname cleanup operation that could hit a swap.
                cleanup_status = "ambiguous"
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(exc, ShutdownServiceError):
                if cleanup_status != "not-needed":
                    raise ShutdownServiceError(
                        f"{exc}; {_private_cleanup_receipt(cleanup_status)}"
                    ) from exc
                raise
            if isinstance(exc, OSError):
                raise ShutdownServiceError(
                    "could not initialize system unit stage directory: "
                    f"{exc}; {_private_cleanup_receipt(cleanup_status)}"
                ) from exc
            raise
    raise ShutdownServiceError("could not reserve a unique system unit stage directory")


def _read_system_file_descriptor(
    descriptor: int,
    *,
    limit: int,
    label: str,
) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise ShutdownServiceError(f"could not rewind {label}: {exc}") from exc
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
        except InterruptedError:
            continue
        except OSError as exc:
            raise ShutdownServiceError(f"could not read {label}: {exc}") from exc
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ShutdownServiceError(f"{label} exceeds the managed size limit")
    return b"".join(chunks)


def _read_system_unit_child(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
    expected: _SystemFileIdentity | None = None,
    owner: tuple[int, int] | None = None,
) -> tuple[bytes, _SystemFileIdentity] | None:
    # A hostile existing leaf can be a FIFO. Open it nonblocking, then reject
    # it by fstat before any read so registration or removal never waits for a
    # peer controlled by the leaf creator.
    flags = (
        os.O_RDONLY
        | _system_nonblock_open_flag()
        | _system_nofollow_open_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShutdownServiceError(f"could not open {label} without following links: {exc}") from exc
    try:
        identity = _system_file_identity(os.fstat(descriptor), label=label)
        if expected is not None and identity != expected:
            raise ShutdownServiceError(f"{label} identity changed")
        if owner is not None and (identity.uid, identity.gid) != owner:
            raise ShutdownServiceError(f"{label} owner does not match system identity")
        named_identity = _system_file_identity(
            _lstat_system_child(parent_descriptor, name, label=label),
            label=label,
        )
        if named_identity != identity:
            raise ShutdownServiceError(f"{label} changed while opening")
        content = _read_system_file_descriptor(
            descriptor,
            limit=_SYSTEM_UNIT_MAX_BYTES,
            label=label,
        )
        final_identity = _system_file_identity(os.fstat(descriptor), label=label)
        if final_identity != identity:
            raise ShutdownServiceError(f"{label} changed while reading")
        final_named_identity = _system_file_identity(
            _lstat_system_child(parent_descriptor, name, label=label),
            label=label,
        )
        if final_named_identity != identity:
            raise ShutdownServiceError(f"{label} changed while reading")
        return content, identity
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_system_file_descriptor(
    descriptor: int,
    content: bytes,
    *,
    label: str,
) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        except OSError as exc:
            raise ShutdownServiceError(f"could not write {label}: {exc}") from exc
        if written <= 0:
            raise ShutdownServiceError(f"could not write {label}: short write")
        view = view[written:]


def _retain_unfinished_stage_file_best_effort(
    stage: _SystemStageDirectory,
    name: str,
    descriptor: int,
) -> None:
    """Keep an incomplete private leaf instead of unlinking it by pathname."""

    try:
        stage.assert_current(phase="during unfinished stage file cleanup")
        identity = _system_file_identity(
            os.fstat(descriptor),
            label="unfinished staged system unit",
        )
        named_identity = _system_file_identity(
            _lstat_system_child(
                stage.descriptor,
                name,
                label="unfinished staged system unit",
            ),
            label="unfinished staged system unit",
        )
        if identity != named_identity:
            return
        _retain_stage_file_if_exact(
            stage,
            name,
            identity,
            phase="unfinished stage file cleanup",
        )
    except (OSError, ShutdownServiceError):
        return


def _stage_system_unit_file(
    stage: _SystemStageDirectory,
    *,
    name: str,
    content: bytes,
    owner: tuple[int, int],
) -> _StagedSystemUnit:
    if len(content) > _SYSTEM_UNIT_MAX_BYTES:
        raise ShutdownServiceError("rendered system checkpoint unit exceeds the managed size limit")
    stage.assert_current(phase=f"before staging {name}")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | _system_nofollow_open_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=stage.descriptor)
    except FileExistsError as exc:
        raise ShutdownServiceError(f"system unit stage file already exists: {name}") from exc
    except OSError as exc:
        raise ShutdownServiceError(f"could not create staged system unit: {exc}") from exc
    try:
        _set_system_descriptor_owner(descriptor, owner, label="staged system unit")
        _set_system_descriptor_mode(descriptor, SYSTEM_UNIT_MODE, label="staged system unit")
        _write_system_file_descriptor(descriptor, content, label="staged system unit")
        _fsync_system_descriptor(descriptor, label="staged system unit")
        identity = _system_file_identity(os.fstat(descriptor), label="staged system unit")
        if (identity.uid, identity.gid) != owner or identity.mode != SYSTEM_UNIT_MODE:
            raise ShutdownServiceError("staged system unit metadata does not match managed contract")
        named_identity = _system_file_identity(
            _lstat_system_child(stage.descriptor, name, label="staged system unit"),
            label="staged system unit",
        )
        if named_identity != identity:
            raise ShutdownServiceError("staged system unit changed while writing")
        stage.assert_current(phase=f"after staging {name}")
        return _StagedSystemUnit(stage, name, descriptor, identity)
    except BaseException:
        _retain_unfinished_stage_file_best_effort(stage, name, descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _retain_stage_file_if_exact(
    stage: _SystemStageDirectory,
    name: str,
    identity: _SystemFileIdentity,
    *,
    phase: str,
) -> None:
    """Atomically retitle a private leaf and retain it for inspection.

    There is no Linux primitive for an atomic ``unlink-if-this-inode``.  A
    check followed by pathname unlink could delete a leaf swapped into the
    stage during that final syscall window.  The transaction therefore keeps
    the leaf under a fresh no-replace retained name.  If the move captured a
    foreign leaf, it remains in the private stage and the receipt becomes
    ambiguous instead of trying to put it back or delete it.
    """

    try:
        stage.assert_current(phase=f"before {phase}")
        observed = _read_system_unit_child(
            stage.descriptor,
            name,
            label=f"system unit stage file {name!r}",
        )
        if observed is None:
            raise ShutdownServiceError(f"system unit stage file disappeared before {phase}")
        if not _same_system_file_after_rename(observed[1], identity):
            raise ShutdownServiceError(f"system unit stage file identity changed before {phase}")
        retained = _new_system_retained_name("file")
        _system_unit_cleanup_probe(f"{phase}:before-stage-file-retain")
        _rename_system_unit(
            stage.descriptor,
            name,
            stage.descriptor,
            retained,
            _RENAME_NOREPLACE,
            label=f"retain staged system unit for {phase}",
        )
        _system_unit_cleanup_probe(f"{phase}:after-stage-file-retain")
        moved = _read_system_unit_child(
            stage.descriptor,
            retained,
            label=f"retained system unit stage file {retained!r}",
        )
        if moved is None or not _same_system_file_after_rename(moved[1], identity):
            raise ShutdownServiceError(
                f"system unit stage file changed while retaining it for {phase}"
            )
        _fsync_system_stage_directory(stage, phase=phase)
    except (OSError, ShutdownServiceError) as exc:
        stage.cleanup_status = "ambiguous"
        raise ShutdownServiceError(
            f"{exc}; private cleanup evidence retained with ambiguous cleanup status"
        ) from exc


def _retain_system_stage_best_effort(stage: _SystemStageDirectory) -> str:
    """Durably retain a private transaction directory without unlinking it.

    The public unit name has already been published, restored, or atomically
    moved out before this runs.  The directory is moved only with
    ``RENAME_NOREPLACE`` to a private retained basename.  A swap at any point
    leaves the foreign object present and reports an ambiguous receipt; this
    helper never calls pathname deletion.
    """

    if stage.retention_attempted:
        return stage.cleanup_status
    stage.retention_attempted = True
    had_ambiguous_leaf = stage.cleanup_status == "ambiguous"
    try:
        stage.assert_current(phase="before stage retention")
        _fsync_system_stage_directory(stage, phase="stage-retention preparation")
        expected = _system_leaf_identity(
            _lstat_system_child(
                stage.parent.descriptor,
                stage.name,
                label="system unit stage directory",
            )
        )
        retained = _new_system_retained_name("directory")
        _system_unit_cleanup_probe("before-stage-directory-retain")
        _rename_system_unit(
            stage.parent.descriptor,
            stage.name,
            stage.parent.descriptor,
            retained,
            _RENAME_NOREPLACE,
            label="retain system unit stage directory",
        )
        _system_unit_cleanup_probe("after-stage-directory-retain")
        moved = _untrusted_system_leaf_identity(
            stage.parent.descriptor,
            retained,
            label="retained system unit stage directory",
        )
        if moved is None or moved != expected:
            stage.cleanup_status = "ambiguous"
            try:
                _fsync_system_unit_parent(stage.parent, phase="ambiguous stage retention")
            except ShutdownServiceError:
                pass
            return stage.cleanup_status
        stage.name = retained
        stage.assert_current(phase="after stage retention")
        _fsync_system_stage_directory(stage, phase="stage-retention")
        _fsync_system_unit_parent(stage.parent, phase="stage-retention")
        if not had_ambiguous_leaf:
            stage.cleanup_status = "retained"
    except (OSError, ShutdownServiceError):
        stage.cleanup_status = "ambiguous"
    return stage.cleanup_status


def _retain_created_system_stage_best_effort(
    parent: _SystemDirectoryAnchor,
    name: str,
    expected: _SystemDirectoryIdentity,
) -> str:
    """Retain a stage that failed before it acquired a stage object.

    The only safe operation available for an uninitialised private directory
    is an atomic no-replace rename.  Do not try to unlink it after a separate
    identity check: another process could replace the name in that final
    window.  If the move captures a different object, leave it retained and
    report the ambiguity rather than moving or deleting it again.
    """

    try:
        parent.assert_current(phase="before unopened stage retention")
        observed = _system_directory_identity(
            _lstat_system_child(
                parent.descriptor,
                name,
                label="unopened system unit stage directory",
            ),
            label="unopened system unit stage directory",
        )
        if observed != expected:
            return "ambiguous"
        retained = _new_system_retained_name("directory")
        _system_unit_cleanup_probe("before-unopened-stage-directory-retain")
        _rename_system_unit(
            parent.descriptor,
            name,
            parent.descriptor,
            retained,
            _RENAME_NOREPLACE,
            label="retain unopened system unit stage directory",
        )
        _system_unit_cleanup_probe("after-unopened-stage-directory-retain")
        moved = _system_directory_identity(
            _lstat_system_child(
                parent.descriptor,
                retained,
                label="retained unopened system unit stage directory",
            ),
            label="retained unopened system unit stage directory",
        )
        if moved != expected:
            try:
                _fsync_system_unit_parent(parent, phase="ambiguous unopened stage retention")
            except ShutdownServiceError:
                pass
            return "ambiguous"
        _fsync_system_unit_parent(parent, phase="unopened stage retention")
        return "retained"
    except (OSError, ShutdownServiceError):
        return "ambiguous"


def _system_unit_install_probe(_phase: str) -> None:
    """Deterministic test seam for hostile filesystem transitions."""


def _system_unit_stage_probe(_phase: str) -> None:
    """Deterministic seam between stage creation and descriptor acquisition."""


def _system_unit_creation_probe(_phase: str) -> None:
    """Deterministic seam immediately after mkdirat and before first lstat."""


def _system_unit_cleanup_probe(_phase: str) -> None:
    """Deterministic test seam for cleanup races in a private stage."""


def _new_system_retained_name(kind: str) -> str:
    return f".{SYSTEM_UNIT_NAME}.retained-{kind}-{os.getpid()}-{uuid.uuid4().hex}"


def _private_cleanup_receipt(status: str) -> str:
    """Describe private transaction evidence without implying it was deleted."""

    if status == "retained":
        return "private cleanup evidence retained"
    if status == "ambiguous":
        return "private cleanup evidence retained or left in place; cleanup status is ambiguous"
    return "no private cleanup evidence needed"


def _restore_untrusted_system_child(
    old_parent_descriptor: int,
    old_name: str,
    new_parent_descriptor: int,
    new_name: str,
    *,
    label: str,
) -> bool:
    """Restore a moved foreign leaf without opening or overwriting it."""

    foreign = _untrusted_system_leaf_identity(
        old_parent_descriptor,
        old_name,
        label=label,
    )
    if foreign is None:
        return False
    try:
        _rename_system_unit(
            old_parent_descriptor,
            old_name,
            new_parent_descriptor,
            new_name,
            _RENAME_NOREPLACE,
            label=label,
        )
    except ShutdownServiceError:
        return False
    restored = _untrusted_system_leaf_identity(
        new_parent_descriptor,
        new_name,
        label=label,
    )
    return restored == foreign


def _open_checkpoint_unit_parent_for_removal(
    directory: Path,
    *,
    owner: tuple[int, int],
    label: str,
) -> _SystemDirectoryAnchor | None:
    """Open an existing unit directory without following any component link."""

    try:
        parent = _open_system_unit_directory(
            directory,
            label=label,
            create_parents=False,
        )
    except _SystemUnitDirectoryMissing:
        return None
    try:
        _validate_system_unit_parent(parent, owner=owner)
        parent.assert_current(phase="before checkpoint unit removal")
        return parent
    except BaseException:
        parent.close()
        raise


def _snapshot_checkpoint_unit_for_removal(
    parent: _SystemDirectoryAnchor,
    *,
    owner: tuple[int, int],
    label: str,
    expected_content: bytes,
) -> _SystemUnitSnapshot:
    """Capture the exact managed leaf eligible for a later removal CAS."""

    parent.assert_current(phase=f"before reading {label} removal preimage")
    observed = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label=label,
        owner=owner,
    )
    if observed is None:
        parent.assert_current(phase=f"after reading absent {label} removal preimage")
        return _SystemUnitSnapshot(b"", None)
    content, identity = observed
    if identity.mode != SYSTEM_UNIT_MODE:
        raise ShutdownServiceError(
            f"{label} mode does not match the managed contract and will not be removed"
        )
    if content != expected_content:
        raise ShutdownServiceError(
            f"{label} does not match the managed contract and will not be removed"
        )
    parent.assert_current(phase=f"after reading {label} removal preimage")
    return _SystemUnitSnapshot(content, identity)


def _assert_checkpoint_unit_removal_snapshot_current(
    parent: _SystemDirectoryAnchor,
    snapshot: _SystemUnitSnapshot,
    *,
    owner: tuple[int, int],
    label: str,
    expected_content: bytes,
    phase: str,
) -> tuple[bytes, _SystemFileIdentity] | None:
    """Bind removal to the exact content and full leaf identity preflighted."""

    parent.assert_current(phase=phase)
    observed = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label=label,
        owner=owner,
    )
    if snapshot.identity is None:
        if observed is not None:
            raise ShutdownServiceError(f"{label} appeared after removal preflight {phase}")
        parent.assert_current(phase=phase)
        return None
    if observed is None:
        raise ShutdownServiceError(f"{label} disappeared after removal preflight {phase}")
    content, identity = observed
    if identity != snapshot.identity:
        raise ShutdownServiceError(f"{label} identity changed after removal preflight {phase}")
    if content != snapshot.content or content != expected_content:
        raise ShutdownServiceError(f"{label} content changed after removal preflight {phase}")
    parent.assert_current(phase=phase)
    return observed


def _assert_checkpoint_unit_absent(
    parent: _SystemDirectoryAnchor,
    *,
    label: str,
    phase: str,
) -> None:
    """Refuse to report removal after any writer has reoccupied the leaf."""

    parent.assert_current(phase=phase)
    observed = _untrusted_system_leaf_identity(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label=f"{label} target {phase}",
    )
    if observed is not None:
        raise ShutdownServiceError(f"{label} target appeared {phase}")
    parent.assert_current(phase=phase)


def _remove_checkpoint_unit_anchored(
    parent: _SystemDirectoryAnchor,
    *,
    owner: tuple[int, int],
    label: str,
    expected_content: bytes,
    snapshot: _SystemUnitSnapshot,
) -> tuple[bool, str]:
    """Durably isolate one exact unit and retain its private removal record."""

    _assert_checkpoint_unit_removal_snapshot_current(
        parent,
        snapshot,
        owner=owner,
        label=label,
        expected_content=expected_content,
        phase=f"before {label} removal",
    )
    if snapshot.identity is None:
        return False, "not-needed"

    stage = _create_system_stage_directory(parent, owner=owner)
    removed_name = "removed-checkpoint-unit"
    try:
        _system_unit_removal_probe("after-removal-preimage")
        current = _assert_checkpoint_unit_removal_snapshot_current(
            parent,
            snapshot,
            owner=owner,
            label=label,
            expected_content=expected_content,
            phase=f"before atomic {label} removal",
        )
        if current is None:  # pragma: no cover - preimage invariant above
            raise ShutdownServiceError(f"{label} disappeared before atomic removal")
        _system_unit_removal_probe("before-removal-cas")
        current = _assert_checkpoint_unit_removal_snapshot_current(
            parent,
            snapshot,
            owner=owner,
            label=label,
            expected_content=expected_content,
            phase=f"immediately before atomic {label} removal",
        )
        if current is None:  # pragma: no cover - preimage invariant above
            raise ShutdownServiceError(f"{label} disappeared before atomic removal")
        _rename_system_unit(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            stage.descriptor,
            removed_name,
            _RENAME_NOREPLACE,
            label=f"isolate {label} for removal",
        )
        try:
            moved = _read_system_unit_child(
                stage.descriptor,
                removed_name,
                label=f"isolated {label}",
                owner=owner,
            )
            if moved is None or not _same_system_file_after_rename(
                moved[1], snapshot.identity
            ):
                raise ShutdownServiceError(f"{label} changed during atomic removal")
        except ShutdownServiceError as exc:
            if not _restore_untrusted_system_child(
                stage.descriptor,
                removed_name,
                parent.descriptor,
                SYSTEM_UNIT_NAME,
                label=f"restore foreign {label} after atomic removal",
            ):
                raise ShutdownServiceError(
                    f"{label} changed during atomic removal; safe restoration failed"
                ) from exc
            raise ShutdownServiceError(f"{label} changed during atomic removal") from exc

        _system_unit_removal_probe("after-removal-isolation")
        _assert_checkpoint_unit_absent(
            parent,
            label=label,
            phase="after atomic removal",
        )
        _fsync_system_stage_directory(stage, phase=f"{label} removal isolation")
        _fsync_system_unit_parent(parent, phase=f"{label} removal")
        _system_unit_removal_probe("before-removal-finalize")
        _assert_checkpoint_unit_absent(
            parent,
            label=label,
            phase="before final removal cleanup",
        )
        # Linux has no unlink-if-this-inode primitive.  Keeping the isolated
        # unit in a private retained stage closes the final cleanup race while
        # the public managed name remains absent.
        return True, _retain_system_stage_best_effort(stage)
    except ShutdownServiceError as exc:
        cleanup_status = _retain_system_stage_best_effort(stage)
        raise ShutdownServiceError(
            f"{exc}; {_private_cleanup_receipt(cleanup_status)}"
        ) from exc
    finally:
        # A failed retention attempt leaves its private stage in place.  It
        # never traverses the public target path and cannot remove a foreign
        # entry that appeared after the identity-pinned move.
        _retain_system_stage_best_effort(stage)
        stage.close()


def _system_unit_removal_probe(_phase: str) -> None:
    """Deterministic test seam for removal compare-and-swap transitions."""


def _snapshot_system_unit(
    parent: _SystemDirectoryAnchor,
    *,
    owner: tuple[int, int],
) -> _SystemUnitSnapshot:
    parent.assert_current(phase="before reading existing system unit")
    observed = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="existing system unit",
        owner=owner,
    )
    if observed is None:
        return _SystemUnitSnapshot(b"", None)
    content, identity = observed
    if identity.mode & 0o022:
        raise ShutdownServiceError("existing system unit must not be group or world writable")
    parent.assert_current(phase="after reading existing system unit")
    return _SystemUnitSnapshot(content, identity)


def _assert_system_unit_snapshot_current(
    parent: _SystemDirectoryAnchor,
    snapshot: _SystemUnitSnapshot,
    *,
    owner: tuple[int, int],
    phase: str,
) -> None:
    parent.assert_current(phase=phase)
    observed = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="system unit target",
        owner=owner,
    )
    if snapshot.identity is None:
        if observed is not None:
            raise ShutdownServiceError(f"system unit target appeared {phase}")
    else:
        if observed is None:
            raise ShutdownServiceError(f"system unit target disappeared {phase}")
        content, identity = observed
        if identity != snapshot.identity or content != snapshot.content:
            raise ShutdownServiceError(f"system unit target changed {phase}")
    parent.assert_current(phase=phase)


def _rename_system_unit(
    old_parent_descriptor: int,
    old_name: str,
    new_parent_descriptor: int,
    new_name: str,
    flags: int,
    *,
    label: str,
) -> None:
    if _RENAMEAT2 is None:
        raise ShutdownServiceError("renameat2 is unavailable; refusing unsafe system unit publish")
    old_value = os.fsencode(old_name)
    new_value = os.fsencode(new_name)
    while True:
        ctypes.set_errno(0)
        result = _RENAMEAT2(
            old_parent_descriptor,
            old_value,
            new_parent_descriptor,
            new_value,
            flags,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EINTR:
            continue
        message = os.strerror(error) if error else "unknown error"
        raise ShutdownServiceError(f"could not {label}: {message}")


def _assert_published_system_unit(
    parent: _SystemDirectoryAnchor,
    staged: _StagedSystemUnit,
    *,
    content: bytes,
    owner: tuple[int, int],
    verify_readback: Callable[[bytes], None],
    phase: str,
) -> None:
    parent.assert_current(phase=phase)
    observed = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="published system unit",
        expected=staged.identity,
        owner=owner,
    )
    if observed is None:
        raise ShutdownServiceError(f"published system unit disappeared {phase}")
    actual, identity = observed
    if actual != content:
        raise ShutdownServiceError(f"published system unit content changed {phase}")
    if identity.mode != SYSTEM_UNIT_MODE or (identity.uid, identity.gid) != owner:
        raise ShutdownServiceError("published system unit metadata does not match managed contract")
    try:
        verify_readback(actual)
    except UnicodeDecodeError as exc:
        raise ShutdownServiceError("published system unit is not valid UTF-8") from exc
    parent.assert_current(phase=phase)


def _restore_foreign_after_exchange(
    parent: _SystemDirectoryAnchor,
    stage: _SystemStageDirectory,
    *,
    stage_name: str,
    expected_parent: _SystemFileIdentity,
    label: str,
) -> bool:
    """Undo an exchange only when it would restore a foreign target intact."""

    try:
        target = _read_system_unit_child(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            label=f"system unit target during {label}",
        )
    except ShutdownServiceError:
        return False
    foreign_identity = _untrusted_system_leaf_identity(
        stage.descriptor,
        stage_name,
        label=f"system unit stage during {label}",
    )
    if (
        target is None
        or foreign_identity is None
        or not _same_system_file_after_rename(target[1], expected_parent)
    ):
        return False
    _rename_system_unit(
        stage.descriptor,
        stage_name,
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        _RENAME_EXCHANGE,
        label=f"restore foreign system unit after {label}",
    )
    restored = _untrusted_system_leaf_identity(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label=f"restored foreign system unit after {label}",
    )
    try:
        returned = _read_system_unit_child(
            stage.descriptor,
            stage_name,
            label=f"returned system unit after {label}",
        )
    except ShutdownServiceError:
        return False
    return (
        restored is not None
        and returned is not None
        and restored == foreign_identity
        and _same_system_file_after_rename(returned[1], expected_parent)
    )


def _publish_staged_system_unit(
    parent: _SystemDirectoryAnchor,
    staged: _StagedSystemUnit,
    snapshot: _SystemUnitSnapshot,
    *,
    owner: tuple[int, int],
) -> None:
    stage = staged.stage
    stage.assert_current(phase="before atomic system unit publish")
    if snapshot.identity is None:
        _rename_system_unit(
            stage.descriptor,
            staged.name,
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            _RENAME_NOREPLACE,
            label="publish new system unit without replacement",
        )
        staged.named = False
        _refresh_staged_system_unit_identity(staged)
        return

    _rename_system_unit(
        stage.descriptor,
        staged.name,
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        _RENAME_EXCHANGE,
        label="atomically exchange system unit",
    )
    _refresh_staged_system_unit_identity(staged)
    try:
        target = _read_system_unit_child(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            label="published system unit after exchange",
        )
    except ShutdownServiceError:
        target = None
    try:
        backup = _read_system_unit_child(
            stage.descriptor,
            staged.name,
            label="system unit backup after exchange",
        )
    except ShutdownServiceError:
        backup = None
    if (
        target is None
        or backup is None
        or target[1] != staged.identity
        or not _same_system_file_after_rename(backup[1], snapshot.identity)
        or backup[0] != snapshot.content
    ):
        # If a target swap happened after the identity CAS, the exchange has
        # moved that foreign leaf into our private stage.  Put it back before
        # failing; do not unlink or overwrite it through a pathname.
        if target is not None and target[1] == staged.identity:
            restored = _restore_foreign_after_exchange(
                parent,
                stage,
                stage_name=staged.name,
                expected_parent=staged.identity,
                label="unexpected publish exchange",
            )
            if restored:
                staged.named = True
                _refresh_staged_system_unit_identity(staged)
        raise ShutdownServiceError("system unit target changed during atomic publish")
    staged.named = False


def _restore_existing_system_unit(
    parent: _SystemDirectoryAnchor,
    staged: _StagedSystemUnit,
    snapshot: _SystemUnitSnapshot,
) -> None:
    if snapshot.identity is None:
        raise ShutdownServiceError("existing system unit rollback requires an existing snapshot")
    stage = staged.stage
    stage.assert_current(phase="before restoring existing system unit")
    current = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="published system unit before rollback",
        expected=staged.identity,
    )
    backup = _read_system_unit_child(
        stage.descriptor,
        staged.name,
        label="system unit backup before rollback",
    )
    if (
        current is None
        or backup is None
        or not _same_system_file_after_rename(backup[1], snapshot.identity)
        or backup[0] != snapshot.content
    ):
        raise ShutdownServiceError("safe rollback refused because the system unit changed")
    _rename_system_unit(
        stage.descriptor,
        staged.name,
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        _RENAME_EXCHANGE,
        label="restore existing system unit",
    )
    _refresh_staged_system_unit_identity(staged)
    target = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="restored system unit",
    )
    replacement = _read_system_unit_child(
        stage.descriptor,
        staged.name,
        label="staged replacement after rollback",
    )
    if (
        target is None
        or replacement is None
        or not _same_system_file_after_rename(target[1], snapshot.identity)
        or target[0] != snapshot.content
        or replacement[1] != staged.identity
    ):
        # A late leaf swap can put a foreign object in the private stage.  If
        # the old target is still named, exchange again to return that object
        # to the caller rather than deleting it.
        if target is not None and _same_system_file_after_rename(target[1], snapshot.identity):
            _restore_foreign_after_exchange(
                parent,
                stage,
                stage_name=staged.name,
                expected_parent=snapshot.identity,
                label="unexpected rollback exchange",
            )
        raise ShutdownServiceError("safe rollback refused because the system unit changed")
    _fsync_system_stage_directory(stage, phase="rollback")
    _fsync_system_unit_parent(parent, phase="rollback")
    # Keep the discarded replacement inside the private transaction stage.
    # It is later atomically retitled as retained evidence rather than
    # unlinked through a pathname.
    staged.named = False


def _restore_new_system_unit_to_absent(
    parent: _SystemDirectoryAnchor,
    staged: _StagedSystemUnit,
    *,
    owner: tuple[int, int],
) -> None:
    """Remove our newly-created target without ever unlinking a swapped leaf."""

    stage = staged.stage
    stage.assert_current(phase="before removing new system unit during rollback")
    current = _read_system_unit_child(
        parent.descriptor,
        SYSTEM_UNIT_NAME,
        label="published system unit before rollback",
        expected=staged.identity,
        owner=owner,
    )
    if current is None:
        raise ShutdownServiceError("safe rollback refused because the system unit disappeared")
    tombstone = _stage_system_unit_file(
        stage,
        name="rollback-tombstone",
        content=b"",
        owner=owner,
    )
    try:
        _rename_system_unit(
            stage.descriptor,
            tombstone.name,
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            _RENAME_EXCHANGE,
            label="isolate new system unit for rollback",
        )
        _refresh_staged_system_unit_identity(staged)
        _refresh_staged_system_unit_identity(tombstone)
        target = _read_system_unit_child(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            label="rollback tombstone",
        )
        isolated = _read_system_unit_child(
            stage.descriptor,
            tombstone.name,
            label="isolated system unit",
        )
        if (
            target is None
            or isolated is None
            or target[1] != tombstone.identity
            or isolated[1] != staged.identity
        ):
            if target is not None and target[1] == tombstone.identity:
                _restore_foreign_after_exchange(
                    parent,
                    stage,
                    stage_name=tombstone.name,
                    expected_parent=tombstone.identity,
                    label="unexpected absent-target rollback exchange",
                )
            raise ShutdownServiceError("safe rollback refused because the system unit changed")
        _rename_system_unit(
            parent.descriptor,
            SYSTEM_UNIT_NAME,
            stage.descriptor,
            "discarded-tombstone",
            _RENAME_NOREPLACE,
            label="remove rollback tombstone without replacement",
        )
        _refresh_staged_system_unit_identity(tombstone)
        discarded = _read_system_unit_child(
            stage.descriptor,
            "discarded-tombstone",
            label="discarded rollback tombstone",
        )
        if discarded is None or discarded[1] != tombstone.identity:
            # A foreign target could have appeared between the exchange and
            # no-replace move.  Restore it only if the public name remains
            # absent; otherwise leave every foreign object untouched.
            try:
                _rename_system_unit(
                    stage.descriptor,
                    "discarded-tombstone",
                    parent.descriptor,
                    SYSTEM_UNIT_NAME,
                    _RENAME_NOREPLACE,
                    label="restore foreign system unit after rollback race",
                )
            except ShutdownServiceError:
                pass
            raise ShutdownServiceError("safe rollback refused because the system unit changed")
        _fsync_system_stage_directory(stage, phase="rollback")
        _fsync_system_unit_parent(parent, phase="rollback")
        # Both isolated files remain in the private stage as rollback
        # evidence.  No final unlink can race a swapped private leaf.
        staged.named = False
    finally:
        tombstone.close()


def _rollback_published_system_unit(
    parent: _SystemDirectoryAnchor,
    staged: _StagedSystemUnit,
    snapshot: _SystemUnitSnapshot,
    *,
    owner: tuple[int, int],
) -> None:
    if snapshot.identity is None:
        _restore_new_system_unit_to_absent(parent, staged, owner=owner)
    else:
        _restore_existing_system_unit(parent, staged, snapshot)


def _retain_unpublished_system_stage(
    stage: _SystemStageDirectory,
    staged: _StagedSystemUnit | None,
) -> str:
    """Retain an unpublished private stage without deleting its contents."""

    del staged
    return _retain_system_stage_best_effort(stage)


def _write_system_checkpoint_unit(
    directory: Path,
    content: bytes,
    *,
    owner: tuple[int, int],
    verify_readback: Callable[[bytes], None],
) -> _SystemUnitWriteReceipt:
    """Durably publish a unit with nofollow anchors and retained evidence.

    The returned receipt state describes the private stage left after the
    transaction.  It never claims that a race-prone pathname deletion ran.
    """

    parent = _open_system_unit_directory(
        directory,
        label="system unit directory",
        create_parents=True,
        owner=owner,
    )
    stage: _SystemStageDirectory | None = None
    staged: _StagedSystemUnit | None = None
    published = False
    try:
        _validate_system_unit_parent(parent, owner=owner)
        stage = _create_system_stage_directory(parent, owner=owner)
        staged = _stage_system_unit_file(
            stage,
            name="payload",
            content=content,
            owner=owner,
        )
        snapshot = _snapshot_system_unit(parent, owner=owner)
        _system_unit_install_probe("after-preimage")
        _assert_system_unit_snapshot_current(
            parent,
            snapshot,
            owner=owner,
            phase="before atomic system unit publish",
        )
        _system_unit_install_probe("after-target-cas")
        _publish_staged_system_unit(parent, staged, snapshot, owner=owner)
        published = True
        _system_unit_install_probe("after-publish")
        _assert_published_system_unit(
            parent,
            staged,
            content=content,
            owner=owner,
            verify_readback=verify_readback,
            phase="before publish durability barrier",
        )
        _fsync_system_stage_directory(stage, phase="publish")
        _fsync_system_unit_parent(parent, phase="publish")
        _assert_published_system_unit(
            parent,
            staged,
            content=content,
            owner=owner,
            verify_readback=verify_readback,
            phase="after publish durability barrier",
        )
        # Retention itself only touches a private name, but a hostile writer
        # can reoccupy the public fixed name while that private evidence is
        # being moved.  Re-read the published leaf before handing success (or
        # a global systemctl call) to the caller.
        cleanup_status = _retain_system_stage_best_effort(stage)
        _assert_published_system_unit(
            parent,
            staged,
            content=content,
            owner=owner,
            verify_readback=verify_readback,
            phase="after private stage retention",
        )
        return _SystemUnitWriteReceipt(staged.identity, cleanup_status)
    except BaseException as exc:
        rollback_error: BaseException | None = None
        if published and staged is not None:
            try:
                _rollback_published_system_unit(parent, staged, snapshot, owner=owner)
            except BaseException as rollback_exc:
                rollback_error = rollback_exc
        if not published and stage is not None:
            _retain_unpublished_system_stage(stage, staged)
        cleanup_status = (
            _retain_system_stage_best_effort(stage)
            if stage is not None
            else "not-needed"
        )
        if rollback_error is not None:
            raise ShutdownServiceError(
                "system checkpoint unit publish failed: "
                f"{exc}; safe rollback failed: {rollback_error}; "
                f"{_private_cleanup_receipt(cleanup_status)}"
            ) from exc
        if isinstance(exc, ShutdownServiceError) and cleanup_status != "not-needed":
            raise ShutdownServiceError(
                f"{exc}; {_private_cleanup_receipt(cleanup_status)}"
            ) from exc
        if isinstance(exc, OSError):
            raise ShutdownServiceError(
                "system checkpoint unit publish failed: "
                f"{exc}; {_private_cleanup_receipt(cleanup_status)}"
            ) from exc
        raise
    finally:
        if staged is not None:
            staged.close()
        if stage is not None:
            _retain_system_stage_best_effort(stage)
            stage.close()
        parent.close()


def install_system_checkpoint_service(
    *,
    executable: str | Path,
    user: str,
    uid: int,
    home: str | Path,
    required_mounts: tuple[str | Path, ...] = (),
    unit_dir: str | Path | None = None,
    enable: bool = True,
) -> ShutdownServiceResult:
    """Install the strict shutdown-ordered system service.

    Callers must run this operation as root. The unit is ordered after the
    complete ``user@UID.service`` while active, which reverses on shutdown and
    guarantees the checkpoint completes before the user manager and its tmux
    scopes are terminated.
    """

    if os.geteuid() != 0:
        raise ShutdownServiceError("system checkpoint service installation requires root")
    resolved = _resolve_t1000_executable(executable)
    directory = _system_unit_directory_path(unit_dir or DEFAULT_SYSTEM_UNIT_DIR)
    path = directory / SYSTEM_UNIT_NAME
    content = render_system_checkpoint_service(
        resolved,
        user=user,
        uid=uid,
        home=home,
        required_mounts=required_mounts,
    )
    verify_system_checkpoint_service(
        content,
        user=user,
        uid=uid,
        home=home,
        required_mounts=required_mounts,
    )

    def verify_readback(data: bytes) -> None:
        verify_system_checkpoint_service(
            data.decode("utf-8"),
            user=user,
            uid=uid,
            home=home,
            required_mounts=required_mounts,
        )

    content_bytes = content.encode("utf-8")
    owner = _system_unit_target_owner()
    if enable:
        # Global ``systemctl`` resolves a fixed unit name through the system
        # search path.  A caller-provided directory cannot be proven to be the
        # leaf that command sees, so refuse it before creating anything.
        _require_canonical_systemctl_directory(directory, action="enable")
    receipt = _write_system_checkpoint_unit(
        directory,
        content_bytes,
        owner=owner,
        verify_readback=verify_readback,
    )
    if enable:
        _assert_installed_system_unit_current(
            directory,
            content=content_bytes,
            identity=receipt.identity,
            owner=owner,
            verify_readback=verify_readback,
            phase="immediately before systemctl daemon-reload",
        )
        _run_systemctl_system(["daemon-reload"])
        _assert_installed_system_unit_current(
            directory,
            content=content_bytes,
            identity=receipt.identity,
            owner=owner,
            verify_readback=verify_readback,
            phase="after systemctl daemon-reload",
        )
        _run_systemctl_system(["enable", "--now", SYSTEM_UNIT_NAME])
        _assert_installed_system_unit_current(
            directory,
            content=content_bytes,
            identity=receipt.identity,
            owner=owner,
            verify_readback=verify_readback,
            phase="after systemctl enable",
        )
    return ShutdownServiceResult(
        "install-system",
        path,
        enable,
        (
            "strict shutdown-ordered checkpoint service installed"
            if enable
            else "strict checkpoint unit written"
        )
        + f"; {_private_cleanup_receipt(receipt.cleanup_status)}",
    )


def uninstall_system_checkpoint_service(
    *,
    unit_dir: str | Path | None = None,
    disable: bool = True,
) -> ShutdownServiceResult:
    if os.geteuid() != 0:
        raise ShutdownServiceError("system checkpoint service removal requires root")
    directory = _system_unit_directory_path(unit_dir or DEFAULT_SYSTEM_UNIT_DIR)
    if disable:
        _require_canonical_systemctl_directory(directory, action="disable")
    path = directory / SYSTEM_UNIT_NAME
    owner = _system_unit_target_owner()
    parent = _open_checkpoint_unit_parent_for_removal(
        directory,
        owner=owner,
        label="system unit directory",
    )
    cleanup_status = "not-needed"
    present = False
    disable_error: ShutdownServiceError | None = None
    try:
        expected_content = _render_system_checkpoint_service_bytes()
        snapshot = (
            _snapshot_checkpoint_unit_for_removal(
                parent,
                owner=owner,
                label="system checkpoint unit",
                expected_content=expected_content,
            )
            if parent is not None
            else _SystemUnitSnapshot(b"", None)
        )
        present = snapshot.exists
        if disable:
            # Bind the global fixed-name request to the exact leaf that this
            # transaction preflighted.  If it was absent, re-open the parent
            # just before the call so a newly-created target cannot be hidden
            # behind an old absence observation.
            if parent is None:
                parent = _open_checkpoint_unit_parent_for_removal(
                    directory,
                    owner=owner,
                    label="system unit directory",
                )
            if parent is not None:
                _assert_checkpoint_unit_removal_snapshot_current(
                    parent,
                    snapshot,
                    owner=owner,
                    label="system checkpoint unit",
                    expected_content=expected_content,
                    phase="immediately before systemctl disable",
                )
            try:
                _run_systemctl_system(["disable", "--now", SYSTEM_UNIT_NAME])
            except ShutdownServiceError as exc:
                if present:
                    raise
                disable_error = exc
        if parent is not None:
            _removed, cleanup_status = _remove_checkpoint_unit_anchored(
                parent,
                owner=owner,
                label="system checkpoint unit",
                expected_content=expected_content,
                snapshot=snapshot,
            )
        # A previously absent parent can appear during the global command.
        # Re-anchor it before reporting an idempotent absence, and also make
        # sure a private-stage retention did not leave a reoccupied public
        # name behind before daemon-reload or the success receipt.
        if parent is None:
            parent = _open_checkpoint_unit_parent_for_removal(
                directory,
                owner=owner,
                label="system unit directory",
            )
        if parent is not None and not present:
            _assert_checkpoint_unit_removal_snapshot_current(
                parent,
                snapshot,
                owner=owner,
                label="system checkpoint unit",
                expected_content=expected_content,
                phase="after systemctl disable",
            )
        if parent is not None and present:
            _assert_checkpoint_unit_absent(
                parent,
                label="system checkpoint unit",
                phase="after private removal retention",
            )
        if disable and disable_error is None:
            if parent is not None:
                _assert_checkpoint_unit_absent(
                    parent,
                    label="system checkpoint unit",
                    phase="immediately before systemctl daemon-reload",
                )
            _run_systemctl_system(["daemon-reload"])
            if parent is None:
                parent = _open_checkpoint_unit_parent_for_removal(
                    directory,
                    owner=owner,
                    label="system unit directory",
                )
            if parent is not None:
                _assert_checkpoint_unit_absent(
                    parent,
                    label="system checkpoint unit",
                    phase="after systemctl daemon-reload",
                )
    finally:
        if parent is not None:
            parent.close()
    detail = (
        "strict shutdown-ordered checkpoint service removed"
        if present
        else "strict shutdown-ordered checkpoint service was already absent"
    )
    if disable_error is not None:
        detail = f"{detail}; systemctl disable was not completed: {disable_error}"
    if cleanup_status != "not-needed":
        detail = f"{detail}; {_private_cleanup_receipt(cleanup_status)}"
    return ShutdownServiceResult(
        "uninstall-system",
        path,
        False,
        detail,
    )
