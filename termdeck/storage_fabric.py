"""Evidence-backed storage attestation for TermDeck's managed runtime.

The managed release launcher may be a small file on the ordinary home
filesystem.  The release tree, session database, private auth scopes, tmux
socket directory, runtime files, and checkpoint images are hot state and must
be backed by a real non-rotational block device.  Environment variables are
reported as advisory launcher metadata; mount and device evidence is the
authority used for admission.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

STORAGE_ATTESTATION_SCHEMA = "termdeck.storage-fabric-attestation.v1"
STORAGE_ATTESTATION_VERSION = 1

# The launcher proves the exact mount, device, filesystem, and rotational
# state.  A canonical SSD path does not prove a storage-fabric label, a
# workspace cell, or bulk-storage masking, so the public attestation uses
# only the direct-root facts that are actually observed.
STORAGE_FABRIC = "direct-root"
STORAGE_CLASS = "non-rotational-block"

# Compatibility names remain importable for older callers, but no longer
# carry the old, unproven fabric or cell labels.
WARP_DRIVES_FABRIC = STORAGE_FABRIC
SSD_CELL_STORAGE_CLASS = STORAGE_CLASS

STORAGE_ENVIRONMENT_KEYS = (
    "CODEX_STORAGE_FABRIC",
    "CODEX_WORKSPACE_STORAGE_CLASS",
)
UNVERIFIED_STORAGE_ENVIRONMENT_KEYS = ("CODEX_BULK_STORAGE_MASKED",)
EXPECTED_STORAGE_ENVIRONMENT = {
    "CODEX_STORAGE_FABRIC": STORAGE_FABRIC,
    "CODEX_WORKSPACE_STORAGE_CLASS": STORAGE_CLASS,
}


@dataclass
class _PinnedDirectory:
    path: Path
    descriptor: int
    device: str
    identity: tuple[int, int]

    @property
    def proc_path(self) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{self.descriptor}")

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor < 0:
            return
        self.descriptor = -1
        os.close(descriptor)


@dataclass
class _ProductionDirectoryBinding:
    path: Path
    expected_device: str
    pinned: _PinnedDirectory | None = None

    def close(self) -> None:
        if self.pinned is not None:
            self.pinned.close()
            self.pinned = None


@dataclass
class _ProductionDatabaseBinding:
    path: Path
    expected_device: str
    parent: _PinnedDirectory | None = None

    def close(self) -> None:
        if self.parent is not None:
            self.parent.close()
            self.parent = None


@dataclass
class DatabaseFileBinding:
    """Descriptor-rooted SQLite file admitted by production storage proof."""

    path: Path
    parent: _PinnedDirectory
    descriptor: int
    expected_device: str
    identity: tuple[int, int]

    @property
    def sqlite_path(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"

    def revalidate(self) -> None:
        _revalidate_pinned_directory(self.parent)
        try:
            metadata = os.stat(
                self.path.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StorageFabricError(
                f"database_root: admitted database identity is no longer reachable: {exc}"
            ) from exc
        if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISREG(
            metadata.st_mode
        ):
            raise StorageFabricError(
                "database_root: admitted database path is no longer a regular file"
            )
        observed_identity = (int(metadata.st_dev), int(metadata.st_ino))
        if observed_identity != self.identity:
            raise StorageFabricError(
                "database_root: database identity changed after production admission"
            )
        if _device_id(metadata.st_dev) != self.expected_device:
            raise StorageFabricError(
                "database_root: database device changed after production admission"
            )

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor >= 0:
            self.descriptor = -1
            os.close(descriptor)
        self.parent.close()


_PRODUCTION_TMUX_RUNTIME_BINDING: (
    _ProductionDirectoryBinding | tuple[Path, str] | None
) = None
_PRODUCTION_DATABASE_BINDING: _ProductionDatabaseBinding | None = None

DEFAULT_MANAGED_ROOT = Path(
    ".local/state/codex-project-homes/runtime/t1000-managed"
)
_DISALLOWED_FILESYSTEMS = frozenset(
    {
        "aufs",
        "cgroup",
        "cgroup2",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "mqueue",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
        "unionfs",
    }
)


class StorageFabricError(RuntimeError):
    """Raised when hot TermDeck state cannot be admitted safely."""

    def __init__(self, reason: str, attestation: "StorageAttestation | None" = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attestation = attestation


def _read_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text(encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sysfs_rotational(device: str) -> str | None:
    """Read rotational state for a major:minor, including device slaves."""

    base = Path("/sys/dev/block") / device
    values: list[str] = []
    candidates = [base / "queue" / "rotational"]
    slaves = base / "slaves"
    try:
        candidates.extend(
            child / "queue" / "rotational"
            for child in sorted(slaves.iterdir(), key=lambda item: item.name)
        )
    except OSError:
        pass
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if value not in {"0", "1"}:
            return None
        values.append(value)
    if not values:
        return None
    return "1" if "1" in values else "0"


def _unescape_mountinfo(value: str) -> str:
    return value.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\134", "\\")


def _as_absolute(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _device_id(value: int) -> str:
    try:
        return f"{os.major(value)}:{os.minor(value)}"
    except (AttributeError, OSError, TypeError, ValueError):
        return ""


def _directory_identity(metadata: object) -> tuple[int, int]:
    return (
        int(getattr(metadata, "st_dev", -1)),
        int(getattr(metadata, "st_ino", -1)),
    )


def _open_pinned_directory(
    path: str | Path,
    *,
    create: bool,
    expected_device: str | None = None,
    require_owner: bool = False,
    final_mode: int | None = None,
) -> _PinnedDirectory:
    """Open an absolute directory without following any path component."""

    requested = _as_absolute(path)
    if requested == Path("/") and (require_owner or final_mode is not None):
        raise StorageFabricError("filesystem root cannot be used as private hot state")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open("/", flags)
    except OSError as exc:
        raise StorageFabricError(
            f"could not bind directory root for {requested}: {exc}"
        ) from exc
    current = Path("/")
    try:
        for component in requested.parts[1:]:
            created = False
            try:
                before = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    raise StorageFabricError(
                        f"path identity disappeared before use: {requested}"
                    )
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise StorageFabricError(
                        f"could not create admitted directory {current / component}: {exc}"
                    ) from exc
                try:
                    before = os.stat(
                        component,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise StorageFabricError(
                        f"could not inspect admitted directory {current / component}: {exc}"
                    ) from exc
                created = True
            except OSError as exc:
                raise StorageFabricError(
                    f"could not inspect admitted directory {current / component}: {exc}"
                ) from exc
            if stat_module.S_ISLNK(before.st_mode):
                raise StorageFabricError(
                    f"path contains a symlink: {current / component}"
                )
            if not stat_module.S_ISDIR(before.st_mode):
                raise StorageFabricError(
                    f"path component is not a directory: {current / component}"
                )
            try:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise StorageFabricError(
                    f"could not bind admitted directory {current / component}: {exc}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                after = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    _directory_identity(before) != _directory_identity(opened)
                    or _directory_identity(after) != _directory_identity(opened)
                ):
                    raise StorageFabricError(
                        f"path identity changed while binding {current / component}"
                    )
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current /= component
            if created and current == requested and final_mode is not None:
                os.fchmod(descriptor, final_mode)

        metadata = os.fstat(descriptor)
        if require_owner and metadata.st_uid != os.geteuid():
            raise StorageFabricError(
                f"admitted directory is not owned by the current user: {requested}"
            )
        if final_mode is not None and stat_module.S_IMODE(metadata.st_mode) != final_mode:
            if metadata.st_uid != os.geteuid():
                raise StorageFabricError(
                    f"admitted directory permissions cannot be hardened: {requested}"
                )
            os.fchmod(descriptor, final_mode)
            metadata = os.fstat(descriptor)
        observed_device = _device_id(metadata.st_dev)
        if expected_device is not None and observed_device != expected_device:
            raise StorageFabricError(
                f"admitted directory device changed for {requested}: "
                f"expected {expected_device}, observed {observed_device or 'unknown'}"
            )
        return _PinnedDirectory(
            path=requested,
            descriptor=descriptor,
            device=observed_device,
            identity=_directory_identity(metadata),
        )
    except Exception:
        os.close(descriptor)
        raise


def _revalidate_pinned_directory(binding: _PinnedDirectory) -> None:
    if binding.descriptor < 0:
        raise StorageFabricError(f"admitted directory binding is closed: {binding.path}")
    current = _open_pinned_directory(
        binding.path,
        create=False,
        expected_device=binding.device,
    )
    try:
        if current.identity != binding.identity:
            raise StorageFabricError(
                f"path identity changed after production admission: {binding.path}"
            )
        retained = os.fstat(binding.descriptor)
        if _directory_identity(retained) != binding.identity:
            raise StorageFabricError(
                f"retained directory identity changed after production admission: {binding.path}"
            )
    finally:
        current.close()


@dataclass(frozen=True)
class MountEvidence:
    """One parsed ``/proc/self/mountinfo`` record."""

    mount_point: Path
    source: str
    filesystem: str
    major: int
    minor: int
    options: tuple[str, ...] = ()

    @property
    def device(self) -> str:
        return f"{self.major}:{self.minor}"

    def as_dict(self) -> dict[str, object]:
        return {
            "mount_point": str(self.mount_point),
            "source": self.source,
            "filesystem": self.filesystem,
            "device": self.device,
            "major": self.major,
            "minor": self.minor,
            "options": list(self.options),
        }


def parse_mountinfo(content: str) -> tuple[MountEvidence, ...]:
    """Parse mountinfo without consulting the host filesystem.

    Malformed records are ignored.  A caller can therefore report a precise
    "no mount evidence" reason instead of accidentally trusting a partial
    line from a changing proc file.
    """

    mounts: list[MountEvidence] = []
    for line in str(content).splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        tail = after.split()
        if len(fields) < 6 or len(tail) < 2:
            continue
        major_minor = fields[2].split(":", 1)
        if len(major_minor) != 2:
            continue
        try:
            major, minor = (int(part) for part in major_minor)
        except ValueError:
            continue
        if major < 0 or minor < 0:
            continue
        mount_point = Path(_unescape_mountinfo(fields[4]))
        if not mount_point.is_absolute():
            continue
        options = tuple(item for item in fields[5].split(",") if item)
        mounts.append(
            MountEvidence(
                mount_point=mount_point,
                source=_unescape_mountinfo(tail[1]),
                filesystem=tail[0],
                major=major,
                minor=minor,
                options=options,
            )
        )
    unique: dict[tuple[str, int, int], MountEvidence] = {}
    for mount in mounts:
        key = (str(mount.mount_point), mount.major, mount.minor)
        previous = unique.get(key)
        if previous is None:
            unique[key] = mount
            continue
        previous_score = (
            int("warp" in previous.source.casefold()),
            int(previous.filesystem.casefold() == "xfs"),
        )
        current_score = (
            int("warp" in mount.source.casefold()),
            int(mount.filesystem.casefold() == "xfs"),
        )
        if current_score >= previous_score:
            unique[key] = mount
    return tuple(unique.values())


@dataclass(frozen=True)
class StorageFabricProbes:
    """Injectable host probes used by the attestation and its unit tests."""

    mountinfo: str | Callable[[], str] | None = None
    stat: Callable[[Path], object] = os.stat
    lstat: Callable[[Path], object] = os.lstat
    rotational: Callable[[str], object] | Mapping[str, object] | None = None
    environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def mountinfo_text(self) -> str:
        source = self.mountinfo
        if source is None:
            return _read_mountinfo()
        return source() if callable(source) else str(source)

    def rotational_value(self, device: str) -> str | None:
        source = self.rotational
        if source is None:
            value = _sysfs_rotational(device)
        elif callable(source):
            value = source(device)
        else:
            value = source.get(device)
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            value = str(value)
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized if normalized in {"0", "1"} else None


@dataclass(frozen=True)
class StoragePathEvidence:
    """Proof (or the exact failure) for one hot or control-plane path."""

    name: str
    requested: str
    resolved: str
    anchor: str
    role: str
    ok: bool
    reason: str
    device: str = ""
    mount_point: str = ""
    source: str = ""
    filesystem: str = ""
    rotational: str | None = None
    storage_class: str = "unverified"

    @property
    def exact_reason(self) -> str:
        return self.reason

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "requested": self.requested,
            "resolved": self.resolved,
            "anchor": self.anchor,
            "role": self.role,
            "ok": self.ok,
            "reason": self.reason,
            "exact_reason": self.reason,
            "device": self.device,
            "mount_point": self.mount_point,
            "source": self.source,
            "filesystem": self.filesystem,
            "rotational": self.rotational,
            "storage_class": self.storage_class,
        }


@dataclass(frozen=True)
class StorageAttestation:
    """Versioned result shared by release, bootstrap, and GUI callers."""

    ok: bool
    reason: str
    paths: tuple[StoragePathEvidence, ...]
    launcher: StoragePathEvidence | None
    environment: dict[str, object]
    production: bool = False
    schema: str = STORAGE_ATTESTATION_SCHEMA
    version: int = STORAGE_ATTESTATION_VERSION
    fabric: str = STORAGE_FABRIC
    storage_class: str = STORAGE_CLASS

    @property
    def exact_reason(self) -> str:
        return self.reason

    @property
    def hot_state_ok(self) -> bool:
        return self.ok

    @property
    def hot_state_would_fallback_to_hdd(self) -> bool:
        return any(
            not path.ok
            or path.rotational == "1"
            or path.storage_class != STORAGE_CLASS
            for path in self.paths
        )

    def require(self) -> "StorageAttestation":
        if not self.ok:
            raise StorageFabricError(self.reason, self)
        return self

    def as_dict(self) -> dict[str, object]:
        rows = [path.as_dict() for path in self.paths]
        path_map = {str(row["name"]): row for row in rows}
        return {
            "schema": self.schema,
            "version": self.version,
            "attestation_version": self.version,
            "fabric": self.fabric,
            "storage_fabric": self.fabric,
            "storage_class": self.storage_class,
            "ok": self.ok,
            "hot_state_ok": self.hot_state_ok,
            "hot_state_would_fallback_to_hdd": self.hot_state_would_fallback_to_hdd,
            "production": self.production,
            "reason": self.reason,
            "exact_reason": self.reason,
            "paths": path_map,
            "path_evidence": rows,
            "launcher": self.launcher.as_dict() if self.launcher else None,
            "launcher_fallback_allowed": True,
            "environment": self.environment,
        }


def _mount_matches(path: Path, mounts: Sequence[MountEvidence]) -> MountEvidence | None:
    candidates: list[tuple[int, int, MountEvidence]] = []
    for index, mount in enumerate(mounts):
        try:
            path.relative_to(mount.mount_point)
        except ValueError:
            continue
        candidates.append((len(mount.mount_point.parts), index, mount))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _nearest_existing(
    path: Path,
    probes: StorageFabricProbes,
) -> tuple[Path | None, str | None]:
    current = path
    anchor: Path | None = None
    while True:
        try:
            metadata = probes.lstat(current)
        except FileNotFoundError:
            if current.parent == current:
                return None, f"path has no existing ancestor: {path}"
            current = current.parent
            continue
        except OSError as exc:
            return None, f"could not inspect path {current}: {exc}"
        mode = int(getattr(metadata, "st_mode", 0))
        if stat_module.S_ISLNK(mode):
            return None, f"path contains a symlink: {current}"
        if not (stat_module.S_ISDIR(mode) or stat_module.S_ISREG(mode)):
            return None, f"path is not a regular file or directory: {current}"
        anchor = current
        break

    current = anchor
    while current.parent != current:
        current = current.parent
        try:
            metadata = probes.lstat(current)
        except OSError as exc:
            return None, f"could not inspect path ancestor {current}: {exc}"
        mode = int(getattr(metadata, "st_mode", 0))
        if stat_module.S_ISLNK(mode):
            return None, f"path contains a symlink: {current}"
        if not stat_module.S_ISDIR(mode):
            return None, f"path ancestor is not a directory: {current}"
    return anchor, None


def _failed_path(
    name: str,
    requested: Path,
    role: str,
    reason: str,
    *,
    resolved: Path | None = None,
    anchor: Path | None = None,
    mount: MountEvidence | None = None,
    device: str = "",
    rotational: str | None = None,
    storage_class: str = "unverified",
) -> StoragePathEvidence:
    return StoragePathEvidence(
        name=name,
        requested=str(requested),
        resolved=str(resolved or requested),
        anchor=str(anchor or ""),
        role=role,
        ok=False,
        reason=f"{name}: {reason}",
        device=device,
        mount_point=str(mount.mount_point) if mount else "",
        source=mount.source if mount else "",
        filesystem=mount.filesystem if mount else "",
        rotational=rotational,
        storage_class=storage_class,
    )


def _attest_path(
    name: str,
    path: str | Path,
    *,
    probes: StorageFabricProbes,
    mounts: Sequence[MountEvidence],
    role: str = "hot",
    require_ssd: bool = True,
) -> StoragePathEvidence:
    requested = _as_absolute(path)
    anchor, error = _nearest_existing(requested, probes)
    if error:
        return _failed_path(name, requested, role, error)
    assert anchor is not None
    try:
        metadata = probes.stat(requested if anchor == requested else anchor)
    except OSError as exc:
        return _failed_path(
            name,
            requested,
            role,
            f"could not read device identity: {exc}",
            anchor=anchor,
        )
    device = _device_id(int(getattr(metadata, "st_dev", -1)))
    if not device:
        return _failed_path(
            name,
            requested,
            role,
            "path has no usable device identity",
            resolved=requested,
            anchor=anchor,
        )
    mount = _mount_matches(requested, mounts) or _mount_matches(anchor, mounts)
    if mount is None:
        return _failed_path(
            name,
            requested,
            role,
            f"no mount evidence for resolved path {requested}",
            resolved=requested,
            anchor=anchor,
            device=device,
        )
    if mount.device != device:
        return _failed_path(
            name,
            requested,
            role,
            f"path device {device} does not match mount device {mount.device}",
            resolved=requested,
            anchor=anchor,
            mount=mount,
            device=device,
        )
    filesystem = mount.filesystem.casefold()
    if filesystem in _DISALLOWED_FILESYSTEMS or mount.source.casefold() in {"overlay", "tmpfs"}:
        reason = f"backing filesystem {mount.filesystem!r} is not an SSD block filesystem"
        if not require_ssd:
            return StoragePathEvidence(
                name=name,
                requested=str(requested),
                resolved=str(requested),
                anchor=str(anchor),
                role=role,
                ok=True,
                reason=f"{name}: control-plane launcher fallback allowed; {reason}",
                device=device,
                mount_point=str(mount.mount_point),
                source=mount.source,
                filesystem=mount.filesystem,
                storage_class="control-plane",
            )
        return _failed_path(
            name,
            requested,
            role,
            reason,
            resolved=requested,
            anchor=anchor,
            mount=mount,
            device=device,
        )
    rotational = probes.rotational_value(device)
    if rotational != "0":
        if not require_ssd:
            return StoragePathEvidence(
                name=name,
                requested=str(requested),
                resolved=str(requested),
                anchor=str(anchor),
                role=role,
                ok=True,
                reason=(
                    f"{name}: control-plane launcher fallback allowed; backing device "
                    f"{device} is {'rotational' if rotational == '1' else 'unverified'}"
                ),
                device=device,
                mount_point=str(mount.mount_point),
                source=mount.source,
                filesystem=mount.filesystem,
                rotational=rotational,
                storage_class="control-plane",
            )
        state = "rotational" if rotational == "1" else "unverified"
        return _failed_path(
            name,
            requested,
            role,
            f"backing device {device} ({mount.source}) is {state}; hot state may fall back to HDD",
            resolved=requested,
            anchor=anchor,
            mount=mount,
            device=device,
            rotational=rotational,
        )
    return StoragePathEvidence(
        name=name,
        requested=str(requested),
        resolved=str(requested),
        anchor=str(anchor),
        role=role,
        ok=True,
        reason=(
            f"{name}: mount {mount.mount_point} on {mount.source} uses "
            f"non-rotational device {device}"
        ),
        device=device,
        mount_point=str(mount.mount_point),
        source=mount.source,
        filesystem=mount.filesystem,
        rotational=rotational,
        storage_class=STORAGE_CLASS,
    )


def _environment_report(
    environment: Mapping[str, str],
    *,
    production: bool,
) -> dict[str, object]:
    del production
    observed = {
        key: str(environment.get(key, "") or "")
        for key in STORAGE_ENVIRONMENT_KEYS
    }
    unverified = {
        key: str(environment.get(key, "") or "")
        for key in UNVERIFIED_STORAGE_ENVIRONMENT_KEYS
        if str(environment.get(key, "") or "")
    }
    matches = observed == EXPECTED_STORAGE_ENVIRONMENT
    present = all(observed.values())
    return {
        "expected": dict(EXPECTED_STORAGE_ENVIRONMENT),
        "observed": observed,
        "unverified": unverified,
        "matches": matches,
        "status": "matching" if matches else ("partial" if present else "missing"),
        "advisory": True,
        "used_for_admission": False,
    }


_PATH_ALIASES = {
    "release": "managed_release_root",
    "release_root": "managed_release_root",
    "managed_release": "managed_release_root",
    "db": "database_root",
    "database": "database_root",
    "database_path": "database_root",
    "auth": "auth_root",
    "tmux": "tmux_runtime",
    "tmux_root": "tmux_runtime",
    "runtime": "runtime_root",
    "checkpoint": "checkpoint_root",
    "checkpoints": "checkpoint_root",
}


def _default_paths(
    managed_root: Path,
    environment: Mapping[str, str],
) -> dict[str, Path]:
    state_root = managed_root.parent / "termdeck"

    def configured(*names: str, default: Path) -> Path:
        for name in names:
            value = str(environment.get(name, "") or "").strip()
            if value:
                return Path(value).expanduser()
        return default

    return {
        "managed_release_root": managed_root,
        "database_root": configured(
            "T1000_DB_PATH",
            "TERMDECK_DB_PATH",
            default=state_root / "sessions.sqlite3",
        ),
        "auth_root": configured(
            "T1000_AUTH_ROOT",
            "TERMDECK_AUTH_ROOT",
            default=state_root / "terminal-auth",
        ),
        "tmux_runtime": configured(
            "TERMDECK_TMUX_RUNTIME",
            "TERMDECK_TMUX_ROOT",
            default=state_root / "tmux",
        ),
        "runtime_root": configured(
            "TERMDECK_RUNTIME_ROOT",
            default=state_root / "runtime",
        ),
        "checkpoint_root": configured(
            "TERMDECK_CHECKPOINT_ROOT",
            default=state_root / "checkpoints",
        ),
    }


def attest_storage_fabric(
    managed_root: str | Path | None = None,
    *,
    db_path: str | Path | None = None,
    database_path: str | Path | None = None,
    auth_root: str | Path | None = None,
    tmux_path: str | Path | None = None,
    tmux_runtime: str | Path | None = None,
    runtime_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    checkpoint_root: str | Path | None = None,
    launcher_path: str | Path | None = None,
    launcher: str | Path | None = None,
    hot_paths: Mapping[str, str | Path] | None = None,
    paths: Mapping[str, str | Path] | None = None,
    probes: StorageFabricProbes | None = None,
    environment: Mapping[str, str] | None = None,
    production: bool = False,
) -> StorageAttestation:
    """Attest all configured hot paths against mount and device evidence.

    The target itself may not exist yet: the nearest existing, non-symlink
    ancestor is used only to obtain the device identity while mount selection
    remains based on the requested canonical path.  This permits safe
    pre-install checks without creating hot state as a side effect.
    """

    active_probes = probes or StorageFabricProbes()
    observed_environment = dict(
        active_probes.environment if environment is None else environment
    )
    configured_root = str(observed_environment.get("T1000_MANAGED_ROOT", "") or "").strip()
    root = _as_absolute(
        managed_root
        or (configured_root if configured_root else Path.home() / DEFAULT_MANAGED_ROOT)
    )
    configured = _default_paths(root, observed_environment)
    explicit = {
        "database_root": database_path or db_path,
        "auth_root": auth_root,
        "tmux_runtime": tmux_runtime or tmux_path,
        "runtime_root": runtime_root or runtime_path,
        "checkpoint_root": checkpoint_root or checkpoint_path,
    }
    for key, value in explicit.items():
        if value is not None:
            configured[key] = Path(value).expanduser()
    for supplied in (hot_paths, paths):
        if supplied:
            for name, value in supplied.items():
                canonical_name = _PATH_ALIASES.get(str(name), str(name))
                configured[canonical_name] = Path(value).expanduser()

    try:
        mounts = parse_mountinfo(active_probes.mountinfo_text())
    except (OSError, UnicodeError) as exc:
        mounts = ()
        mount_error = f"mountinfo could not be read: {exc}"
    else:
        mount_error = ""

    evidence: list[StoragePathEvidence] = []
    if mount_error:
        for name, path in configured.items():
            evidence.append(
                _failed_path(name, _as_absolute(path), "hot", mount_error)
            )
    else:
        for name, path in configured.items():
            evidence.append(
                _attest_path(
                    name,
                    path,
                    probes=active_probes,
                    mounts=mounts,
                )
            )

    launcher_value = launcher_path or launcher
    launcher_evidence = None
    if launcher_value is not None:
        if mount_error:
            launcher_evidence = _failed_path(
                "launcher",
                _as_absolute(launcher_value),
                "control-plane",
                mount_error,
            )
        else:
            launcher_evidence = _attest_path(
                "launcher",
                launcher_value,
                probes=active_probes,
                mounts=mounts,
                role="control-plane",
                require_ssd=False,
            )

    environment_report = _environment_report(
        observed_environment,
        production=bool(production),
    )
    # Environment variables are descriptive metadata only.  Production
    # admission is based on the path/device proof above, so a launcher
    # started by a desktop or systemd manager without these optional
    # variables remains truthful and usable.
    failures = [path.reason for path in evidence if not path.ok]
    if failures:
        reason = failures[0] if len(failures) == 1 else (
            f"{len(failures)} hot paths failed storage attestation; first failure: {failures[0]}"
        )
    else:
        reason = (
            f"all {len(evidence)} hot paths resolve to non-rotational "
            f"{STORAGE_FABRIC}/{STORAGE_CLASS} storage; "
            "environment labels are advisory"
        )
    return StorageAttestation(
        ok=not failures,
        reason=reason,
        paths=tuple(evidence),
        launcher=launcher_evidence,
        environment=environment_report,
        production=bool(production),
    )


def storage_attestation(*args: object, **kwargs: object) -> StorageAttestation:
    """Compatibility alias for callers that use the noun first."""

    return attest_storage_fabric(*args, **kwargs)


def _bind_production_tmux_runtime(
    attestation: StorageAttestation,
    *,
    environment: MutableMapping[str, str] | None = None,
    materialize: bool = False,
) -> Path:
    """Bind tmux clients to the exact runtime admitted by production storage."""

    evidence = next(
        (path for path in attestation.paths if path.name == "tmux_runtime"),
        None,
    )
    if (
        evidence is None
        or not evidence.ok
        or evidence.storage_class != STORAGE_CLASS
        or not evidence.device
    ):
        raise StorageFabricError(
            "tmux_runtime: production storage attestation did not admit a "
            "non-rotational block runtime",
            attestation,
        )
    runtime = Path(evidence.resolved)
    if not runtime.is_absolute():
        raise StorageFabricError(
            "tmux_runtime: production storage attestation returned a non-absolute path",
            attestation,
        )

    binding = _ProductionDirectoryBinding(runtime, evidence.device)
    if materialize:
        binding.pinned = _open_pinned_directory(
            runtime,
            create=True,
            expected_device=evidence.device,
            require_owner=True,
            final_mode=0o700,
        )

    global _PRODUCTION_TMUX_RUNTIME_BINDING
    previous = _PRODUCTION_TMUX_RUNTIME_BINDING
    _PRODUCTION_TMUX_RUNTIME_BINDING = binding
    if isinstance(previous, _ProductionDirectoryBinding):
        previous.close()
    target = os.environ if environment is None else environment
    target["TERMDECK_TMUX_RUNTIME"] = str(runtime)
    target["TMUX_TMPDIR"] = str(runtime)
    target.pop("TMUX", None)
    return runtime


def _bind_production_database(
    attestation: StorageAttestation,
    *,
    materialize: bool = False,
) -> Path:
    evidence = next(
        (path for path in attestation.paths if path.name == "database_root"),
        None,
    )
    if (
        evidence is None
        or not evidence.ok
        or evidence.storage_class != STORAGE_CLASS
        or not evidence.device
    ):
        raise StorageFabricError(
            "database_root: production storage attestation did not admit a "
            "non-rotational block database",
            attestation,
        )
    database = Path(evidence.resolved)
    if not database.is_absolute() or database.name in {"", ".", ".."}:
        raise StorageFabricError(
            "database_root: production storage attestation returned an invalid path",
            attestation,
        )
    binding = _ProductionDatabaseBinding(database, evidence.device)
    if materialize:
        binding.parent = _open_pinned_directory(
            database.parent,
            create=True,
            expected_device=evidence.device,
            require_owner=True,
            final_mode=0o700,
        )

    global _PRODUCTION_DATABASE_BINDING
    previous = _PRODUCTION_DATABASE_BINDING
    _PRODUCTION_DATABASE_BINDING = binding
    if previous is not None:
        previous.close()
    return database


def _materialize_tmux_binding(
    binding: _ProductionDirectoryBinding,
) -> _PinnedDirectory:
    if binding.pinned is None:
        binding.pinned = _open_pinned_directory(
            binding.path,
            create=True,
            expected_device=binding.expected_device,
            require_owner=True,
            final_mode=0o700,
        )
    else:
        _revalidate_pinned_directory(binding.pinned)
    return binding.pinned


def _materialize_database_parent(
    binding: _ProductionDatabaseBinding,
) -> _PinnedDirectory:
    if binding.parent is None:
        binding.parent = _open_pinned_directory(
            binding.path.parent,
            create=True,
            expected_device=binding.expected_device,
            require_owner=True,
            final_mode=0o700,
        )
    else:
        _revalidate_pinned_directory(binding.parent)
    return binding.parent


def pin_production_database(
    path: str | Path,
) -> DatabaseFileBinding | None:
    """Pin the admitted database inode immediately before SQLite opens it."""

    parent = pin_production_database_parent(path)
    if parent is None:
        return None
    binding = _PRODUCTION_DATABASE_BINDING
    assert binding is not None
    requested = binding.path
    descriptor = -1
    try:
        descriptor = os.open(
            requested.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent.descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise StorageFabricError(
                "database_root: admitted database is not a regular file"
            )
        if _device_id(metadata.st_dev) != binding.expected_device:
            raise StorageFabricError(
                "database_root: database device changed before SQLite open"
            )
        os.fchmod(descriptor, 0o600)
        result = DatabaseFileBinding(
            path=requested,
            parent=parent,
            descriptor=descriptor,
            expected_device=binding.expected_device,
            identity=_directory_identity(metadata),
        )
        result.revalidate()
        return result
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        parent.close()
        raise


def pin_production_database_parent(
    path: str | Path,
) -> _PinnedDirectory | None:
    """Duplicate the retained parent descriptor for a production DB path."""

    binding = _PRODUCTION_DATABASE_BINDING
    if binding is None:
        return None
    requested = _as_absolute(path)
    if requested != binding.path:
        raise StorageFabricError(
            "database_root: requested database drifted from production admission"
        )
    admitted_parent = _materialize_database_parent(binding)
    _revalidate_pinned_directory(admitted_parent)
    parent_descriptor = os.dup(admitted_parent.descriptor)
    parent_metadata = os.fstat(parent_descriptor)
    parent = _PinnedDirectory(
        path=admitted_parent.path,
        descriptor=parent_descriptor,
        device=_device_id(parent_metadata.st_dev),
        identity=_directory_identity(parent_metadata),
    )
    return parent


def tmux_subprocess_environment(
    *,
    materialize: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return a child environment bound to admitted production tmux storage.

    Development processes that have not performed production storage admission
    retain the historical inherited environment. Once admission succeeds, the
    stable logical variables remain fixed to the attested path; materialized
    tmux children use a procfd alias for the retained directory descriptor.
    """

    binding = _PRODUCTION_TMUX_RUNTIME_BINDING
    if binding is None:
        return None
    if isinstance(binding, tuple):
        runtime, expected_device = binding
        binding = _ProductionDirectoryBinding(
            _as_absolute(runtime),
            str(expected_device),
        )
        globals()["_PRODUCTION_TMUX_RUNTIME_BINDING"] = binding
    runtime = binding.path
    expected_device = binding.expected_device
    source = os.environ if environment is None else environment
    configured_runtime = str(source.get("TERMDECK_TMUX_RUNTIME", "") or "")
    configured_tmpdir = str(source.get("TMUX_TMPDIR", "") or "")
    expected = str(runtime)
    if configured_runtime != expected or configured_tmpdir != expected:
        raise StorageFabricError(
            "tmux_runtime: production tmux environment drifted from the attested SSD path"
        )

    pinned = None
    if materialize:
        try:
            pinned = _materialize_tmux_binding(binding)
            metadata = os.fstat(pinned.descriptor)
            observed_device = _device_id(metadata.st_dev)
            socket_root_name = f"tmux-{os.geteuid()}"
            try:
                socket_root_metadata = os.stat(
                    socket_root_name,
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                socket_root_metadata = None
            if socket_root_metadata is not None:
                if stat_module.S_ISLNK(
                    socket_root_metadata.st_mode
                ) or not stat_module.S_ISDIR(socket_root_metadata.st_mode):
                    raise OSError("tmux socket root is not a directory")
                if socket_root_metadata.st_uid != os.geteuid():
                    raise OSError("tmux socket root is not owned by the current user")
                if _device_id(socket_root_metadata.st_dev) != expected_device:
                    raise OSError("tmux socket root is not on the attested device")
                socket_root_descriptor = os.open(
                    socket_root_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=pinned.descriptor,
                )
                try:
                    try:
                        socket_metadata = os.stat(
                            "default",
                            dir_fd=socket_root_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        socket_metadata = None
                    if socket_metadata is not None:
                        if not stat_module.S_ISSOCK(socket_metadata.st_mode):
                            raise OSError("tmux default socket is not a Unix socket")
                        if _device_id(socket_metadata.st_dev) != expected_device:
                            raise OSError("tmux default socket is not on the attested device")
                finally:
                    os.close(socket_root_descriptor)
        except OSError as exc:
            raise StorageFabricError(
                f"tmux_runtime: could not materialize the attested runtime {runtime}: {exc}"
            ) from exc
        if observed_device != expected_device:
            raise StorageFabricError(
                "tmux_runtime: materialized runtime device does not match storage attestation"
            )

    child = dict(source)
    child["TERMDECK_TMUX_RUNTIME"] = expected
    child["TMUX_TMPDIR"] = str(pinned.proc_path) if pinned is not None else expected
    child.pop("TMUX", None)
    return child


def require_storage_fabric(*args: object, **kwargs: object) -> StorageAttestation:
    """Return an admitted attestation or fail closed with its exact reason."""

    attestation = attest_storage_fabric(*args, **kwargs).require()
    if attestation.production:
        materialize = kwargs.get("probes") is None
        _bind_production_database(attestation, materialize=materialize)
        try:
            _bind_production_tmux_runtime(
                attestation,
                materialize=materialize,
            )
        except Exception:
            global _PRODUCTION_DATABASE_BINDING
            if _PRODUCTION_DATABASE_BINDING is not None:
                _PRODUCTION_DATABASE_BINDING.close()
                _PRODUCTION_DATABASE_BINDING = None
            raise
    return attestation


def require_hot_storage(*args: object, **kwargs: object) -> StorageAttestation:
    """Readable alias used by production install/bootstrap call sites."""

    return require_storage_fabric(*args, **kwargs)


class StorageFabric:
    """Small state-free facade for dependency-injected callers."""

    def __init__(self, *, probes: StorageFabricProbes | None = None) -> None:
        self.probes = probes

    def attest(self, *args: object, **kwargs: object) -> StorageAttestation:
        kwargs.setdefault("probes", self.probes)
        return attest_storage_fabric(*args, **kwargs)

    def require(self, *args: object, **kwargs: object) -> StorageAttestation:
        return self.attest(*args, **kwargs).require()


__all__ = [
    "DatabaseFileBinding",
    "DEFAULT_MANAGED_ROOT",
    "EXPECTED_STORAGE_ENVIRONMENT",
    "MountEvidence",
    "SSD_CELL_STORAGE_CLASS",
    "STORAGE_ATTESTATION_SCHEMA",
    "STORAGE_ATTESTATION_VERSION",
    "STORAGE_CLASS",
    "STORAGE_ENVIRONMENT_KEYS",
    "UNVERIFIED_STORAGE_ENVIRONMENT_KEYS",
    "STORAGE_FABRIC",
    "StorageAttestation",
    "StorageFabric",
    "StorageFabricError",
    "StorageFabricProbes",
    "StoragePathEvidence",
    "WARP_DRIVES_FABRIC",
    "attest_storage_fabric",
    "parse_mountinfo",
    "pin_production_database",
    "pin_production_database_parent",
    "require_hot_storage",
    "require_storage_fabric",
    "storage_attestation",
    "tmux_subprocess_environment",
]
