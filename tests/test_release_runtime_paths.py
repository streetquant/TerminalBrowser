from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from termdeck import build_info, release_manager as manager, storage_fabric as storage
from test_release_manager import write_manifest, write_wheel
from test_storage_fabric import _ssd_fixture


_HOT_ENVIRONMENT = (
    "TERMDECK_STATE_ROOT",
    "T1000_DB_PATH",
    "TERMDECK_DB_PATH",
    "T1000_AUTH_ROOT",
    "TERMDECK_AUTH_ROOT",
    "TERMDECK_TMUX_RUNTIME",
    "TERMDECK_TMUX_ROOT",
    "TERMDECK_RUNTIME_ROOT",
    "TERMDECK_CHECKPOINT_ROOT",
    "TMUX_TMPDIR",
    "TMUX",
)
_CANONICAL_ROOT = Path("/ssd/t1000-managed")
_CANONICAL_STATE = Path("/ssd/state")
_STALE_ENVIRONMENT = {
    "TERMDECK_STATE_ROOT": "/ssd/old-state",
    "T1000_DB_PATH": "/ssd/other-database/sessions.sqlite3",
    "TERMDECK_AUTH_ROOT": "/ssd/other-auth",
    "TERMDECK_TMUX_RUNTIME": "/ssd/other-tmux",
    "TERMDECK_TMUX_ROOT": "/ssd/older-tmux",
    "TERMDECK_RUNTIME_ROOT": "/ssd/other-runtime",
    "TERMDECK_CHECKPOINT_ROOT": "/ssd/other-checkpoints",
}


def _state_paths(root: Path, state: Path) -> dict[str, Path]:
    return {
        "managed_release_root": root,
        "database_root": state / "sessions.sqlite3",
        "auth_root": state / "terminal-auth",
        "tmux_runtime": state / "tmux",
        "runtime_root": state / "runtime",
        "checkpoint_root": state / "checkpoints",
    }


def _observed_paths(attestation: storage.StorageAttestation) -> dict[str, Path]:
    return {item.name: Path(item.requested) for item in attestation.paths}


def _set_storage_environment(monkeypatch: pytest.MonkeyPatch, *, stale: bool) -> None:
    for name in _HOT_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    if stale:
        for name, value in _STALE_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize("operation", ["install", "migration", "rollback"])
@pytest.mark.parametrize("stale", [False, True], ids=["clean-env", "stale-env"])
def test_canonical_release_operations_admit_the_client_state_paths(
    monkeypatch: pytest.MonkeyPatch, operation: str, stale: bool
) -> None:
    _set_storage_environment(monkeypatch, stale=stale)
    monkeypatch.setattr(manager, "_DEPLOYMENT_MANAGED_ROOT", _CANONICAL_ROOT)
    monkeypatch.setattr(manager, "_DEPLOYMENT_STATE_ROOT", _CANONICAL_STATE)
    monkeypatch.setattr(manager, "_production_manages_autostart", lambda *_a, **_kw: False)
    _filesystem, probes = _ssd_fixture()
    observed: list[storage.StorageAttestation] = []

    def stop_after_admission(**kwargs: object) -> None:
        admitted = storage.attest_storage_fabric(
            **kwargs, probes=probes, environment=dict(os.environ)
        ).require()
        observed.append(admitted)
        raise storage.StorageFabricError("test stopped after admission")

    monkeypatch.setattr(storage, "require_hot_storage", stop_after_admission)
    # Stop before artifact reads, locks, or publication, while exercising the
    # actual public operation's mandatory production-admission entry point.
    with pytest.raises(manager.ReleaseError, match="test stopped after admission"):
        if operation == "rollback":
            manager.rollback_release(root=_CANONICAL_ROOT, production=False)
        else:
            migration = (
                {"migrate_from_root": "/retired/managed", "migrate_from_bin_dir": "/retired/bin"}
                if operation == "migration"
                else {}
            )
            manager.install_release(
                "/unused.whl", "/unused.json", root=_CANONICAL_ROOT,
                production=False, **migration,
            )
    assert len(observed) == 1
    assert observed[0].production is True
    assert _observed_paths(observed[0]) == _state_paths(_CANONICAL_ROOT, _CANONICAL_STATE)


@pytest.mark.parametrize(
    "unavailable", ["database_root", "auth_root", "tmux_runtime", "runtime_root", "checkpoint_root"]
)
def test_canonical_admission_rejects_invalid_state_despite_safe_inherited_alternatives(
    monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    _set_storage_environment(monkeypatch, stale=True)
    monkeypatch.setattr(manager, "_DEPLOYMENT_MANAGED_ROOT", _CANONICAL_ROOT)
    monkeypatch.setattr(manager, "_DEPLOYMENT_STATE_ROOT", _CANONICAL_STATE)
    filesystem, probes = _ssd_fixture()
    filesystem.directory(str(_CANONICAL_STATE), (253, 1))
    rejected_path = _state_paths(_CANONICAL_ROOT, _CANONICAL_STATE)[unavailable]
    filesystem.symlink(str(rejected_path), (253, 1))

    def require_fixture(**kwargs: object) -> storage.StorageAttestation:
        return storage.attest_storage_fabric(
            **kwargs, probes=probes, environment=dict(os.environ)
        ).require()

    monkeypatch.setattr(storage, "require_hot_storage", require_fixture)
    with pytest.raises(manager.ReleaseError, match=unavailable):
        manager._require_managed_storage(manager.release_layout(root=_CANONICAL_ROOT))


@pytest.mark.parametrize("stale", [False, True], ids=["default", "configured"])
def test_other_managed_layouts_preserve_storage_environment_semantics(
    monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    _set_storage_environment(monkeypatch, stale=stale)
    _filesystem, probes = _ssd_fixture()
    root = Path("/ssd/disposable-managed")

    def require_fixture(**kwargs: object) -> storage.StorageAttestation:
        return storage.attest_storage_fabric(
            **kwargs, probes=probes, environment=dict(os.environ)
        ).require()

    monkeypatch.setattr(storage, "require_hot_storage", require_fixture)
    observed = manager._require_managed_storage(manager.release_layout(root=root))
    expected = _state_paths(root, Path("/ssd/termdeck"))
    if stale:
        expected.update({
            "database_root": Path(_STALE_ENVIRONMENT["T1000_DB_PATH"]),
            "auth_root": Path(_STALE_ENVIRONMENT["TERMDECK_AUTH_ROOT"]),
            "tmux_runtime": Path(_STALE_ENVIRONMENT["TERMDECK_TMUX_RUNTIME"]),
            "runtime_root": Path(_STALE_ENVIRONMENT["TERMDECK_RUNTIME_ROOT"]),
            "checkpoint_root": Path(_STALE_ENVIRONMENT["TERMDECK_CHECKPOINT_ROOT"]),
        })
    assert _observed_paths(observed) == expected


def _install_probe_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, canonical: bool
) -> manager.ReleaseLayout:
    """Run the generated bootstrap with fixture evidence, never real admission."""
    layout = manager.release_layout(root=tmp_path / "managed", bin_dir=tmp_path / "bin")
    if canonical:
        # Only the desktop rendering's state selection is substituted.  The
        # release manager keeps the ordinary disposable wrapper/unit outputs.
        original = manager._deployment_state_root
        monkeypatch.setattr(
            manager, "_deployment_state_root",
            lambda candidate: tmp_path / "state" if candidate.root == layout.root else original(candidate),
        )
    fixture_admission = r'''
_real_require_hot_storage = require_hot_storage
def require_hot_storage(**kwargs):
    import json
    from types import SimpleNamespace
    device = _device_id(os.stat(Path(__file__).parent).st_dev)
    def fixture_lstat(path):
        if str(path) == os.environ.get("TERMDECK_TEST_REJECT_STORAGE"):
            return SimpleNamespace(st_mode=stat_module.S_IFLNK | 0o777)
        return os.lstat(path)
    probes = StorageFabricProbes(
        mountinfo=f"1 0 {device} / / rw - ext4 /dev/test-storage rw",
        lstat=fixture_lstat,
        rotational={device: "0"},
        environment=dict(os.environ),
    )
    admitted = _real_require_hot_storage(**kwargs, probes=probes)
    os.environ["TERMDECK_TEST_ADMISSION"] = json.dumps(admitted.as_dict())
    return admitted
'''
    wheel = tmp_path / "t1000-0.2.13-py3-none-any.whl"
    info = write_wheel(
        wheel, version="0.2.13", extra_members={
            "termdeck/build_info.py": Path(build_info.__file__).read_bytes(),
            "termdeck/storage_fabric.py": Path(storage.__file__).read_bytes() + fixture_admission.encode(),
            "termdeck/cli.py": (
                "import json, os\n"
                "def main():\n"
                "    print(json.dumps({\n"
                "        'admission': json.loads(os.environ['TERMDECK_TEST_ADMISSION']),\n"
                "        'tmux': os.environ['TERMDECK_TMUX_RUNTIME'],\n"
                "        'tmpdir': os.environ['TMUX_TMPDIR'],\n"
                "        'inherited_tmux': os.environ.get('TMUX'),\n"
                "    }, sort_keys=True))\n"
                "    return 0\n"
            ).encode(),
        },
    )
    manifest = wheel.with_suffix(".json")
    write_manifest(manifest, wheel, info)
    manager.install_release(wheel, manifest, root=layout.root, bin_dir=layout.bin_dir)
    return layout


@pytest.mark.parametrize("canonical", [True, False], ids=["canonical", "configured-layout"])
@pytest.mark.parametrize("override", [None, "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_ROOT"])
def test_generated_launcher_admission_keeps_the_expected_tmux_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canonical: bool, override: str | None
) -> None:
    layout = _install_probe_launcher(tmp_path, monkeypatch, canonical=canonical)
    environment = {name: value for name, value in os.environ.items() if name not in _HOT_ENVIRONMENT}
    environment["TMUX"] = "/tmp/foreign/default,123,0"
    environment["TMUX_TMPDIR"] = str(tmp_path / "foreign-tmpdir")
    if override:
        environment[override] = str(tmp_path / "other-tmux")
        environment["TERMDECK_STATE_ROOT"] = str(tmp_path / "configured-state")
    result = subprocess.run(
        [str(layout.launcher_path)], env=environment, text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    expected_state = tmp_path / ("state" if canonical else ("configured-state" if override else "termdeck"))
    expected_tmux = tmp_path / "other-tmux" if override and not canonical else expected_state / "tmux"
    paths = observed["admission"]["paths"]
    assert paths["database_root"]["requested"] == str(expected_state / "sessions.sqlite3")
    assert paths["auth_root"]["requested"] == str(expected_state / "terminal-auth")
    assert paths["runtime_root"]["requested"] == str(expected_state / "runtime")
    assert paths["checkpoint_root"]["requested"] == str(expected_state / "checkpoints")
    assert paths["tmux_runtime"]["requested"] == str(expected_tmux)
    assert observed["tmux"] == observed["tmpdir"] == str(expected_tmux)
    assert observed["inherited_tmux"] is None


def test_generated_canonical_launcher_refuses_invalid_canonical_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _install_probe_launcher(tmp_path, monkeypatch, canonical=True)
    environment = {name: value for name, value in os.environ.items() if name not in _HOT_ENVIRONMENT}
    environment["TERMDECK_TMUX_RUNTIME"] = str(tmp_path / "safe-alternative")
    environment["TERMDECK_TEST_REJECT_STORAGE"] = str(tmp_path / "state" / "tmux")
    result = subprocess.run(
        [str(layout.launcher_path)], env=environment, text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 78
    assert "refused unsafe storage" in result.stderr
    assert "tmux_runtime" in result.stderr
    assert not result.stdout
