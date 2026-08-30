from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from termdeck.storage_fabric import (
    STORAGE_CLASS,
    STORAGE_FABRIC,
    StorageFabricError,
    StorageFabricProbes,
    attest_storage_fabric,
    parse_mountinfo,
    require_storage_fabric,
)


def _mount(
    mount_point: str,
    device: tuple[int, int],
    *,
    filesystem: str = "xfs",
    source: str = "/dev/mapper/warp_drives-exec",
) -> str:
    major, minor = device
    return (
        f"100 1 {major}:{minor} / {mount_point} rw,nosuid,nodev - "
        f"{filesystem} {source} rw"
    )


class FixtureFilesystem:
    def __init__(self) -> None:
        self.nodes: dict[str, int] = {}
        self.devices: dict[str, int] = {}

    def directory(self, path: str, device: tuple[int, int]) -> None:
        self.nodes[path] = stat.S_IFDIR | 0o700
        self.devices[path] = os.makedev(*device)

    def file(self, path: str, device: tuple[int, int]) -> None:
        self.nodes[path] = stat.S_IFREG | 0o700
        self.devices[path] = os.makedev(*device)

    def symlink(self, path: str, device: tuple[int, int]) -> None:
        self.nodes[path] = stat.S_IFLNK | 0o777
        self.devices[path] = os.makedev(*device)

    def lstat(self, path: Path) -> SimpleNamespace:
        key = str(path)
        mode = self.nodes.get(key)
        if mode is None:
            raise FileNotFoundError(key)
        return SimpleNamespace(st_mode=mode)

    def stat(self, path: Path) -> SimpleNamespace:
        key = str(path)
        device = self.devices.get(key)
        if device is None:
            nearest = path
            while str(nearest) not in self.devices and nearest.parent != nearest:
                nearest = nearest.parent
            device = self.devices.get(str(nearest))
        if device is None:
            raise FileNotFoundError(key)
        return SimpleNamespace(st_dev=device)


def _probes(
    filesystem: FixtureFilesystem,
    mounts: str,
    rotational: dict[str, object],
    environment: dict[str, str] | None = None,
) -> StorageFabricProbes:
    return StorageFabricProbes(
        mountinfo=mounts,
        stat=filesystem.stat,
        lstat=filesystem.lstat,
        rotational=rotational,
        environment=environment or {},
    )


def _hot_paths() -> dict[str, str]:
    return {
        "release": "/ssd/t1000-managed",
        "db": "/ssd/termdeck/sessions.sqlite3",
        "auth": "/ssd/termdeck/terminal-auth",
        "tmux": "/ssd/termdeck/tmux",
        "runtime": "/ssd/termdeck/runtime",
        "checkpoint": "/ssd/termdeck/checkpoints",
    }


def _ssd_fixture() -> tuple[FixtureFilesystem, StorageFabricProbes]:
    filesystem = FixtureFilesystem()
    filesystem.directory("/ssd", (253, 1))
    mounts = _mount("/ssd", (253, 1))
    probes = _probes(filesystem, mounts, {"253:1": "0"})
    return filesystem, probes


def test_mountinfo_normalizes_duplicate_mapper_labels_by_device() -> None:
    content = "\n".join(
        (
            _mount("/ssd", (253, 1), source="/dev/mapper/codexpro_vg-exec"),
            _mount("/ssd", (253, 1), source="/dev/mapper/warp_drives-exec"),
            _mount("/hdd", (8, 17), filesystem="ext4", source="/dev/sdb1"),
        )
    )

    records = parse_mountinfo(content)

    assert len(records) == 2
    ssd = next(record for record in records if record.mount_point == Path("/ssd"))
    assert ssd.device == "253:1"
    assert ssd.source == "/dev/mapper/warp_drives-exec"


def test_attestation_accepts_nonexistent_hot_children_on_verified_ssd() -> None:
    filesystem, probes = _ssd_fixture()

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        probes=probes,
    )

    assert result.ok
    assert result.hot_state_ok
    assert not result.hot_state_would_fallback_to_hdd
    assert result.fabric == STORAGE_FABRIC
    assert result.storage_class == STORAGE_CLASS
    assert result.environment["status"] == "missing"
    assert result.environment["used_for_admission"] is False
    assert all(row.ok for row in result.paths)
    assert all(row.anchor == "/ssd" for row in result.paths)
    assert filesystem.nodes["/ssd"] & stat.S_IFMT(stat.S_IFDIR)


def test_mount_proof_overrides_conflicting_shell_environment() -> None:
    filesystem, _ = _ssd_fixture()
    probes = _probes(
        filesystem,
        _mount("/ssd", (253, 1)),
        {
            "253:1": "0",
        },
        environment={
            "CODEX_STORAGE_FABRIC": STORAGE_FABRIC,
            "CODEX_WORKSPACE_STORAGE_CLASS": "legacy-control-root",
            "CODEX_BULK_STORAGE_MASKED": "1",
        },
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        probes=probes,
    )

    assert result.ok
    assert result.environment["matches"] is False
    assert result.environment["advisory"] is True


def test_attestation_rejects_rotational_hot_path_with_exact_reason() -> None:
    filesystem, probes = _ssd_fixture()
    filesystem.directory("/hdd", (8, 17))
    mounts = "\n".join(
        (
            _mount("/ssd", (253, 1)),
            _mount("/hdd", (8, 17), filesystem="ext4", source="/dev/sdb1"),
        )
    )
    probes = _probes(
        filesystem,
        mounts,
        {
            "253:1": "0",
            "8:17": "1",
        },
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths={**_hot_paths(), "db": "/hdd/termdeck/sessions.sqlite3"},
        probes=probes,
        production=True,
    )

    assert not result.ok
    assert result.production
    assert result.hot_state_would_fallback_to_hdd
    assert "database_root" in result.reason
    assert "rotational" in result.reason
    assert result.as_dict()["paths"]["database_root"]["exact_reason"] == result.reason.split("; first failure: ")[-1]


def test_attestation_rejects_overlay_even_if_rotational_probe_says_ssd() -> None:
    filesystem = FixtureFilesystem()
    filesystem.directory("/overlay", (0, 79))
    probes = _probes(
        filesystem,
        _mount("/overlay", (0, 79), filesystem="overlay", source="overlay"),
        {"0:79": "0"},
    )

    result = attest_storage_fabric(
        "/overlay/t1000-managed",
        hot_paths={"release": "/overlay/t1000-managed"},
        probes=probes,
    )

    assert not result.ok
    assert "not an SSD block filesystem" in result.reason


def test_attestation_rejects_path_and_mount_device_mismatch() -> None:
    filesystem = FixtureFilesystem()
    filesystem.directory("/ssd", (8, 17))
    probes = _probes(
        filesystem,
        _mount("/ssd", (253, 1)),
        {"8:17": "0", "253:1": "0"},
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths={"release": "/ssd/t1000-managed"},
        probes=probes,
    )

    assert not result.ok
    assert "does not match mount device" in result.reason


def test_symlinked_hot_path_fails_closed_before_device_admission() -> None:
    filesystem = FixtureFilesystem()
    filesystem.symlink("/ssd", (253, 1))
    probes = _probes(
        filesystem,
        _mount("/ssd", (253, 1)),
        {"253:1": "0"},
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths={"release": "/ssd/t1000-managed"},
        probes=probes,
    )

    assert not result.ok
    assert "symlink" in result.reason


def test_hdd_launcher_is_reported_but_does_not_block_verified_hot_state() -> None:
    filesystem, _ = _ssd_fixture()
    filesystem.file("/hdd/t1000", (8, 17))
    filesystem.directory("/hdd", (8, 17))
    mounts = "\n".join(
        (
            _mount("/ssd", (253, 1)),
            _mount("/hdd", (8, 17), filesystem="ext4", source="/dev/sdb1"),
        )
    )
    probes = _probes(
        filesystem,
        mounts,
        {
            "253:1": "0",
            "8:17": "1",
        },
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        launcher_path="/hdd/t1000",
        probes=probes,
    )

    assert result.ok
    assert result.launcher is not None
    assert result.launcher.ok
    assert result.launcher.storage_class == "control-plane"
    assert "fallback allowed" in result.launcher.reason


def test_require_storage_fabric_raises_the_same_exact_reason() -> None:
    filesystem = FixtureFilesystem()
    filesystem.directory("/hdd", (8, 17))
    probes = _probes(
        filesystem,
        _mount("/hdd", (8, 17), filesystem="ext4", source="/dev/sdb1"),
        {"8:17": "1"},
    )

    with pytest.raises(StorageFabricError, match="rotational") as raised:
        require_storage_fabric(
            "/hdd/t1000-managed",
            hot_paths={"release": "/hdd/t1000-managed"},
            probes=probes,
        )

    assert raised.value.attestation is not None
    assert raised.value.reason == raised.value.attestation.reason
