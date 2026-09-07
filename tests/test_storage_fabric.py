from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import termdeck.storage_fabric as storage_fabric
from termdeck.storage_fabric import (
    EXPECTED_STORAGE_ENVIRONMENT,
    STORAGE_CLASS,
    STORAGE_FABRIC,
    StorageFabricError,
    StorageFabricProbes,
    attest_storage_fabric,
    parse_mountinfo,
    require_storage_fabric,
    tmux_subprocess_environment,
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
        self.nodes: dict[str, int] = {"/": stat.S_IFDIR | 0o755}
        self.devices: dict[str, int] = {"/": os.makedev(0, 1)}

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


def _qualified_environment(**extra: str) -> dict[str, str]:
    return {**EXPECTED_STORAGE_ENVIRONMENT, **extra}


def _ssd_fixture(
    *,
    qualified: bool = False,
) -> tuple[FixtureFilesystem, StorageFabricProbes]:
    filesystem = FixtureFilesystem()
    filesystem.directory("/ssd", (253, 1))
    mounts = _mount("/ssd", (253, 1))
    probes = _probes(
        filesystem,
        mounts,
        {"253:1": "0"},
        environment=_qualified_environment() if qualified else None,
    )
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


def test_production_attestation_accepts_direct_root_without_launcher_metadata() -> None:
    _filesystem, probes = _ssd_fixture()

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        probes=probes,
        production=True,
    )

    assert result.ok
    assert result.environment["status"] == "missing"
    assert result.environment["used_for_admission"] is False
    assert result.environment["advisory"] is True
    assert "environment labels are advisory" in result.reason


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
        environment=_qualified_environment(),
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


def test_existing_descendant_cannot_hide_a_symlink_ancestor() -> None:
    filesystem = FixtureFilesystem()
    filesystem.directory("/ssd", (253, 1))
    filesystem.symlink("/ssd/routed", (253, 1))
    filesystem.directory("/ssd/routed/existing", (253, 1))
    probes = _probes(
        filesystem,
        _mount("/ssd", (253, 1)),
        {"253:1": "0"},
    )

    result = attest_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths={"db": "/ssd/routed/existing/sessions.sqlite3"},
        probes=probes,
    )

    assert not result.ok
    assert "path contains a symlink: /ssd/routed" in result.reason


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


def test_production_admission_overrides_inherited_tmux_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _filesystem, probes = _ssd_fixture(qualified=True)
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        None,
    )
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_DATABASE_BINDING",
        None,
    )
    monkeypatch.setenv("TMUX_TMPDIR", "/hdd/inherited-tmux")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")

    result = require_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        probes=probes,
        production=True,
    )

    assert result.ok
    assert os.environ["TERMDECK_TMUX_RUNTIME"] == "/ssd/termdeck/tmux"
    assert os.environ["TMUX_TMPDIR"] == "/ssd/termdeck/tmux"
    assert "TMUX" not in os.environ
    child = tmux_subprocess_environment()
    assert child is not None
    assert child["TERMDECK_TMUX_RUNTIME"] == "/ssd/termdeck/tmux"
    assert child["TMUX_TMPDIR"] == "/ssd/termdeck/tmux"
    assert "TMUX" not in child


def test_production_tmux_environment_rejects_post_attestation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _filesystem, probes = _ssd_fixture(qualified=True)
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        None,
    )
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_DATABASE_BINDING",
        None,
    )
    require_storage_fabric(
        "/ssd/t1000-managed",
        hot_paths=_hot_paths(),
        probes=probes,
        production=True,
    )
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp")

    with pytest.raises(StorageFabricError, match="drifted"):
        tmux_subprocess_environment()


def test_production_admission_rejects_configured_hdd_tmux_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = FixtureFilesystem()
    filesystem.directory("/ssd", (253, 1))
    filesystem.directory("/hdd", (8, 17))
    probes = _probes(
        filesystem,
        "\n".join(
            (
                _mount("/ssd", (253, 1)),
                _mount("/hdd", (8, 17), filesystem="ext4", source="/dev/sdb1"),
            )
        ),
        {"253:1": "0", "8:17": "1"},
        environment=_qualified_environment(
            TERMDECK_TMUX_RUNTIME="/hdd/termdeck/tmux"
        ),
    )
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        None,
    )
    monkeypatch.setenv("TMUX_TMPDIR", "/hdd/inherited-tmux")

    with pytest.raises(StorageFabricError, match="tmux_runtime.*rotational"):
        require_storage_fabric(
            "/ssd/t1000-managed",
            probes=probes,
            production=True,
        )

    assert os.environ["TMUX_TMPDIR"] == "/hdd/inherited-tmux"
    assert storage_fabric._PRODUCTION_TMUX_RUNTIME_BINDING is None


def test_materialized_tmux_runtime_must_match_attested_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "tmux-runtime"
    device = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        (runtime, device),
    )
    monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(runtime))
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))

    child = tmux_subprocess_environment(materialize=True)

    assert child is not None
    assert runtime.is_dir()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert os.makedev(*map(int, device.split(":"))) == runtime.stat().st_dev
    assert child["TERMDECK_TMUX_RUNTIME"] == str(runtime)
    assert Path(child["TMUX_TMPDIR"]).samefile(runtime)
    assert child["TMUX_TMPDIR"] != str(runtime)


def test_materialized_tmux_runtime_rejects_device_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "tmux-runtime"
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        (runtime, "8:17"),
    )
    monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(runtime))
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))

    with pytest.raises(StorageFabricError, match="device changed|expected"):
        tmux_subprocess_environment(materialize=True)


def test_pinned_tmux_route_cannot_be_redirected_by_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "tmux-runtime"
    original = tmp_path / "attested-runtime"
    attacker = tmp_path / "attacker-runtime"
    attacker.mkdir()
    device = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        (runtime, device),
    )
    monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(runtime))
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))

    child = tmux_subprocess_environment(materialize=True)
    assert child is not None
    runtime.rename(original)
    runtime.symlink_to(attacker, target_is_directory=True)

    subprocess.run(
        ["/usr/bin/touch", f"{child['TMUX_TMPDIR']}/route-probe"],
        env=child,
        check=True,
    )
    assert (original / "route-probe").is_file()
    assert not (attacker / "route-probe").exists()
    with pytest.raises(StorageFabricError, match="identity changed|symlink"):
        tmux_subprocess_environment(materialize=True)

    binding = storage_fabric._PRODUCTION_TMUX_RUNTIME_BINDING
    assert isinstance(binding, storage_fabric._ProductionDirectoryBinding)
    binding.close()


def test_materialized_tmux_runtime_rejects_non_socket_default_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "tmux-runtime"
    device = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    socket_root = runtime / f"tmux-{os.geteuid()}"
    socket_root.mkdir(parents=True)
    (socket_root / "default").write_text("not-a-socket", encoding="utf-8")
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        (runtime, device),
    )
    monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(runtime))
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))

    with pytest.raises(StorageFabricError, match="not a Unix socket"):
        tmux_subprocess_environment(materialize=True)
