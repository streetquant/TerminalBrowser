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
from typing import Callable, Mapping, Sequence

STORAGE_ATTESTATION_SCHEMA = "termdeck.storage-fabric-attestation.v1"
STORAGE_ATTESTATION_VERSION = 1
STORAGE_FABRIC = "warp-drives-v1"
STORAGE_CLASS = "ssd-cell"

WARP_DRIVES_FABRIC = STORAGE_FABRIC
SSD_CELL_STORAGE_CLASS = STORAGE_CLASS

STORAGE_ENVIRONMENT_KEYS = (
    "CODEX_STORAGE_FABRIC",
    "CODEX_WORKSPACE_STORAGE_CLASS",
    "CODEX_BULK_STORAGE_MASKED",
)
EXPECTED_STORAGE_ENVIRONMENT = {
    "CODEX_STORAGE_FABRIC": STORAGE_FABRIC,
    "CODEX_WORKSPACE_STORAGE_CLASS": STORAGE_CLASS,
    "CODEX_BULK_STORAGE_MASKED": "1",
}

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
            not path.ok or path.rotational == "1" or path.storage_class != STORAGE_CLASS
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
        return current, None


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


def _environment_report(environment: Mapping[str, str]) -> dict[str, object]:
    observed = {key: str(environment.get(key, "") or "") for key in STORAGE_ENVIRONMENT_KEYS}
    matches = observed == EXPECTED_STORAGE_ENVIRONMENT
    present = all(observed.values())
    return {
        "expected": dict(EXPECTED_STORAGE_ENVIRONMENT),
        "observed": observed,
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

    failures = [path.reason for path in evidence if not path.ok]
    if failures:
        reason = failures[0] if len(failures) == 1 else (
            f"{len(failures)} hot paths failed storage attestation; first failure: {failures[0]}"
        )
    else:
        reason = (
            f"all {len(evidence)} hot paths resolve to non-rotational "
            f"{STORAGE_FABRIC}/{STORAGE_CLASS} storage"
        )
    return StorageAttestation(
        ok=not failures,
        reason=reason,
        paths=tuple(evidence),
        launcher=launcher_evidence,
        environment=_environment_report(observed_environment),
        production=bool(production),
    )


def storage_attestation(*args: object, **kwargs: object) -> StorageAttestation:
    """Compatibility alias for callers that use the noun first."""

    return attest_storage_fabric(*args, **kwargs)


def require_storage_fabric(*args: object, **kwargs: object) -> StorageAttestation:
    """Return an admitted attestation or fail closed with its exact reason."""

    return attest_storage_fabric(*args, **kwargs).require()


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
    "DEFAULT_MANAGED_ROOT",
    "EXPECTED_STORAGE_ENVIRONMENT",
    "MountEvidence",
    "SSD_CELL_STORAGE_CLASS",
    "STORAGE_ATTESTATION_SCHEMA",
    "STORAGE_ATTESTATION_VERSION",
    "STORAGE_CLASS",
    "STORAGE_ENVIRONMENT_KEYS",
    "STORAGE_FABRIC",
    "StorageAttestation",
    "StorageFabric",
    "StorageFabricError",
    "StorageFabricProbes",
    "StoragePathEvidence",
    "WARP_DRIVES_FABRIC",
    "attest_storage_fabric",
    "parse_mountinfo",
    "require_hot_storage",
    "require_storage_fabric",
    "storage_attestation",
]
