"""Real wheel, copied installation and frozen recovery-program regressions.

Set TERMDECK_TEST_LEGACY_RELEASE to a *read-only* historical release directory
to exercise the shipped c9d620 artifact.  All extraction, selection and inverse
writes still take place under pytest's disposable directory.  Without that
input an explicitly registered synthetic profile exercises the same protocol.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from termdeck import legacy_activation as legacy
from termdeck import release_manager as manager
from termdeck import web_activation as activation
from termdeck import service_wrappers as wrappers
from termdeck.build_info import MANAGED_STATE_SCHEMA, canonical_json_bytes, sha256_file
from test_release_manager import write_manifest, write_wheel
from test_web_activation import _storage_attestation
from test_web_activation import _inject_post_install_failure


@pytest.fixture
def case(tmp_path, monkeypatch):
    package = Path(manager.__file__).parent
    members = {
        "termdeck/" + str(p.relative_to(package)): p.read_bytes()
        for p in package.rglob("*.py") if p.name != "__init__.py"
    }
    # Only the disposable CLI's external storage gate is isolated. Artifact,
    # RECORD, frozen tree, bootstrap and publication validation stay real.
    members["termdeck/storage_fabric.py"] += b"\n\ndef require_hot_storage(**kwargs):\n    return None\n"
    supplied = os.environ.get("TERMDECK_TEST_LEGACY_RELEASE")
    if supplied:
        source = Path(supplied)
        manifest = json.loads((source / "manifest.json").read_bytes())
        assert manifest == manager._PRE_WRAPPER_V1_MANIFEST
        old_manifest = source / "manifest.json"
        old_wheel = source / "artifacts" / manifest["wheel"]
    else:
        old_wheel = tmp_path / "old-artifact" / "t1000-0.2.21-py3-none-any.whl"
        old_members = {name: data for name, data in members.items() if name != "termdeck/service_wrappers.py"}
        info = write_wheel(old_wheel, seed="a", version="0.2.21", extra_members=old_members)
        old_manifest = old_wheel.with_suffix(".release.json")
        manifest = write_manifest(old_manifest, old_wheel, info)
        monkeypatch.setattr(manager, "_PRE_WRAPPER_V1_MANIFEST", manifest)
        # The independent candidate loader must know the same explicit test
        # profile; there is no production fallback on missing module bytes.
        members["termdeck/release_manager.py"] += ("\n_PRE_WRAPPER_V1_MANIFEST = " + repr(manifest) + "\n").encode()
    old = manager.release_layout(root=tmp_path / "retired" / "managed", bin_dir=tmp_path / "retired" / "bin")
    verified = manager.verify_wheel(old_wheel, manifest_path=old_manifest)
    with manager.release_lock(old):
        manager._stage_release(old, verified, old_manifest)
        interpreter = Path(sys.executable).resolve()
        bundle = manager._derive_release_activation_bundle(old, manifest["release_id"], interpreter)
        state = manager.validate_managed_state({
            "schema": MANAGED_STATE_SCHEMA, "generation": 1,
            "current_release_id": manifest["release_id"], "previous_release_id": None,
            "manifest_sha256": sha256_file(old.releases_dir / manifest["release_id"] / "manifest.json"),
            "python_executable": str(interpreter), "bootstrap_sha256": bundle.bootstrap_sha256,
            "launcher_path": str(old.launcher_path), "launcher_sha256": bundle.launcher_sha256,
            "legacy_launcher": None,
        })
        manager._atomic_write_bytes(old.bootstrap_path, bundle.bootstrap, mode=0o755)
        manager._atomic_write_bytes(old.launcher_path, bundle.launcher, mode=0o755)
        manager._atomic_write_bytes(old.state_path, canonical_json_bytes(state), mode=0o600)
    root = tmp_path / "copied"
    paths = activation.ActivationPaths(
        target=package.parent, evidence=root / "state/recovery/activation-v2",
        runtime=root / "termdeck-lan", database=root / "state/sessions.sqlite3",
        managed_root=root / "managed", managed_bin=root / "bin", unit_dir=root / "units",
    )
    shutil.copytree(old.root, paths.managed_root)
    shutil.copytree(old.bin_dir, paths.managed_bin)
    paths.unit_dir.mkdir()
    paths.runtime.mkdir()
    paths.database.parent.mkdir()
    with sqlite3.connect(paths.database) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('preserved-session', '/work')")
    wheel = tmp_path / "candidate" / "t1000-0.2.21-py3-none-any.whl"
    info = write_wheel(wheel, seed="b", version="0.2.21", extra_members=members)
    manifest_path = wheel.with_suffix(".release.json")
    candidate = write_manifest(manifest_path, wheel, info)
    tx, receipt = activation.new_transaction(paths, source={"commit": "b" * 40, "tree": "b" * 40, "branch": "fixture", "origin_main": "a" * 40})
    receipt.update(status="artifacts-staged", package={"wheel": str(wheel), "manifest": str(manifest_path), "release_id": candidate["release_id"]},
                   legacy_path_migration_request={"source_root": str(old.root), "source_bin_dir": str(old.bin_dir)})
    activation._write(tx / "receipt.json", receipt)
    copied = manager.release_layout(root=paths.managed_root, bin_dir=paths.managed_bin)
    original = {p: p.read_bytes() for p in (copied.state_path, copied.bootstrap_path, copied.launcher_path)}
    sentinels = {root / "saved-tabs.json": b'{"cwd":"/work","saved":["a","b"]}', root / "auth-sentinel": b"synthetic private state"}
    for path, data in sentinels.items():
        path.write_bytes(data)
    monkeypatch.setattr(manager, "_require_managed_storage", lambda _layout: _storage_attestation())
    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    return SimpleNamespace(paths=paths, tx=tx, receipt=receipt, old=old, copied=copied,
                           original=original, sentinels=sentinels, wheel=wheel, manifest=manifest_path,
                           candidate=candidate, original_release=manifest["release_id"])


def stage_bootstrap(case):
    p, tx, receipt = case.paths, case.tx, case.receipt
    receipt["legacy_bootstrap"] = legacy.prepare(p, tx, receipt)
    receipt["old_release_id"] = case.original_release
    receipt["preimages"] = {
        "wrapper:" + str(path): activation._preimage(path, tx, "w-" + str(index))
        for index, path in enumerate(activation._wrapper_publication_paths(p))
    }
    receipt["preimages"]["unit:" + activation.WATCHDOG_UNIT] = activation._preimage(p.unit_dir / activation.WATCHDOG_UNIT, tx, "watchdog")
    activation._persist(p, tx, receipt, "mutation-start")
    return receipt


def install(case, receipt):
    before, after = activation._release_publication_callbacks(
        case.paths, case.tx, receipt, receipt["preimages"], key="package_publication",
        operation="install", expected_release_id=case.candidate["release_id"],
        checkpoint=lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name),
    )
    return activation._install(case.paths, receipt["package"], migration=receipt["legacy_bootstrap"]["migration"], before_publication=before, after_publication=after)


def test_legacy_migration_real_launcher_and_exact_original_inverse(case):
    receipt = stage_bootstrap(case)
    result = install(case, receipt)
    assert result["path_migration"]["historical_paths_written"] is False
    help_result = subprocess.run([str(case.copied.launcher_path), "--help"], text=True, capture_output=True, timeout=40)
    assert help_result.returncode == 0, help_result.stderr
    assert "usage:" in help_result.stdout.lower()
    info = subprocess.run([str(case.copied.launcher_path), "release", "info", "--root", str(case.copied.root), "--bin-dir", str(case.copied.bin_dir), "--verify"], text=True, capture_output=True, timeout=40)
    assert info.returncode == 0, info.stderr
    assert json.loads(info.stdout)["ok"] is True
    # Read back from the durable receipt to avoid relying on Python aliases.
    receipt = activation._read(case.tx / "receipt.json")
    legacy.restore_package(case.paths, case.tx, receipt, lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name))
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
    assert all(path.read_bytes() == content for path, content in case.sentinels.items())
    legacy.restore_package(case.paths, case.tx, activation._read(case.tx / "receipt.json"), lambda _name: None)


def test_legacy_inverse_accepts_real_wrapper_config_lifecycle(case):
    receipt = stage_bootstrap(case)
    install(case, receipt)
    layout = wrappers.resolve_layout(**activation._wrapper_kwargs(case.paths))
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-backend.service"]
    wrappers._record_start_identity(spec, layout, case.candidate["release_id"], [sys.executable, "-c", "pass"])
    value, snapshot = wrappers._read_unit_config_snapshot(layout.config_path(spec), spec=spec)
    value.update(pid=None, start_ticks=None, exe=None, argv=[], command_sha256=None, state="stopped")
    wrappers._atomic_write_json(snapshot.path, value, mode=0o600, snapshot=snapshot)
    receipt = activation._read(case.tx / "receipt.json")
    legacy.restore_package(case.paths, case.tx, receipt,
                           lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name))
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))


def test_frozen_recovery_program_runs_after_checkout_is_unavailable(case):
    receipt = stage_bootstrap(case)
    receipt["status"] = "rolled-back"
    activation._write(case.tx / "receipt.json", receipt)
    command = [sys.executable, "-I", "-B", str(case.tx / "legacy-recovery.py"), "--watchdog"]
    for _ in range(2):
        result = subprocess.run(command, cwd="/", text=True, capture_output=True, timeout=40,
                                env={**os.environ, "PYTHONPATH": "/nonexistent", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-legacy-fixture"})
        assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in case.original} == case.original


@pytest.mark.parametrize("change", ["state-inode", "runner", "capsule", "request"])
def test_legacy_bootstrap_rejects_replacements(case, change):
    receipt = stage_bootstrap(case)
    if change == "state-inode":
        content = case.copied.state_path.read_bytes()
        activation._atomic(case.copied.state_path, content, 0o600)
        with pytest.raises(activation.WebActivationError, match="selection changed"):
            legacy.arm(case.paths, case.tx, receipt, receipt["preimages"], lambda _name: None)
    elif change == "runner":
        runner = case.tx / "legacy-recovery.py"
        activation._atomic(runner, runner.read_bytes(), 0o500)
        with pytest.raises(activation.WebActivationError):
            legacy._data(case.paths, case.tx, receipt)
    elif change == "capsule":
        site = legacy._capsule_layout(case.tx).releases_dir / case.candidate["release_id"] / "site"
        path = site / "termdeck/service_wrappers.py"
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\n# modified\n")
        with pytest.raises(manager.ReleaseError):
            legacy._data(case.paths, case.tx, receipt)
    else:
        receipt["legacy_bootstrap"]["arguments"][1] = "/foreign/transaction"
        with pytest.raises(activation.WebActivationError, match="paths differ"):
            legacy._data(case.paths, case.tx, receipt)


def test_unsupported_same_version_missing_module_is_not_legacy(case, monkeypatch):
    monkeypatch.setattr(manager, "_PRE_WRAPPER_V1_MANIFEST", {**manager._PRE_WRAPPER_V1_MANIFEST, "release_id": "f" * 64})
    with pytest.raises(manager.ReleaseError, match="missing termdeck/service_wrappers.py"):
        manager._release_wrapper_execution_kwargs(case.copied, case.original_release)
    with pytest.raises(activation.WebActivationError, match="supported historical release"):
        legacy.prepare(case.paths, case.tx, case.receipt)


def test_custom_evidence_is_refused_before_snapshot_or_journal(case, monkeypatch):
    paths = dataclasses.replace(case.paths, evidence=case.paths.evidence.parent / "custom")
    tx, receipt = activation.new_transaction(paths, source=case.receipt["source"])
    receipt.update(status="artifacts-staged", package=case.receipt["package"])
    activation._write(tx / "receipt.json", receipt)
    before = (tx / "receipt.json").read_bytes()
    monkeypatch.setattr(activation, "snapshot_database", lambda *_args: pytest.fail("snapshot before evidence refusal"))
    with pytest.raises(activation.WebActivationError, match="canonical activation-v2"):
        activation.execute(paths, tx)
    assert (tx / "receipt.json").read_bytes() == before
    assert sorted(path.name for path in tx.iterdir()) == ["receipt.json"]


def test_legacy_requires_both_explicit_historical_paths(case):
    case.receipt.pop("legacy_path_migration_request")
    with pytest.raises(activation.WebActivationError, match="explicit --migrate-from"):
        legacy.prepare(case.paths, case.tx, case.receipt)


def test_complete_execute_failure_restores_legacy_install(case, monkeypatch):
    _inject_post_install_failure(monkeypatch)
    with pytest.raises(RuntimeError, match="post-install activation failure"):
        activation.execute(case.paths, case.tx)
    receipt = activation._read(case.tx / "receipt.json")
    assert receipt["status"] == "rolled-back", receipt.get("rollback_error")
    assert receipt["watchdog"]["arm_phase"] == "legacy-capsule"
    assert receipt["legacy_bootstrap"]["inverse_complete"] is True
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
    assert all(path.read_bytes() == content for path, content in case.sentinels.items())


@pytest.mark.parametrize("boundary", ["intent", "published", "committed", "inverse"])
def test_hard_process_exit_and_restart_preserve_exact_legacy_inverse(case, boundary):
    receipt = stage_bootstrap(case)
    if boundary == "inverse":
        install(case, receipt)
    pid = os.fork()
    if pid == 0:
        try:
            if boundary == "inverse":
                receipt = activation._read(case.tx / "receipt.json")
                def checkpoint(name):
                    activation._checkpoint_publication(case.paths, case.tx, receipt, name)
                    if name.startswith("legacy:package-inverse:"):
                        os._exit(87)
                legacy.restore_package(case.paths, case.tx, receipt, checkpoint)
            else:
                before, after = activation._release_publication_callbacks(
                    case.paths, case.tx, receipt, receipt["preimages"], key="package_publication",
                    operation="install", expected_release_id=case.candidate["release_id"],
                    checkpoint=lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name),
                )
                def intent(value):
                    before(value)
                    if boundary == "intent":
                        os._exit(87)
                def published(value):
                    after(value)
                    if boundary == "published":
                        os._exit(87)
                activation._install(case.paths, receipt["package"], migration=receipt["legacy_bootstrap"]["migration"], before_publication=intent, after_publication=published)
                os._exit(87 if boundary == "committed" else 89)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(88)
        os._exit(89)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 87
    receipt = activation._read(case.tx / "receipt.json")
    def checkpoint(name):
        activation._checkpoint_publication(case.paths, case.tx, receipt, name)
    activation._recover_pending_release_publications(case.paths, case.tx, receipt, checkpoint=checkpoint)
    legacy.restore_package(case.paths, case.tx, receipt, checkpoint)
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
    assert all(path.read_bytes() == content for path, content in case.sentinels.items())


def test_frozen_watchdog_recovers_committed_migration_in_private_service_namespace(case, monkeypatch):
    """The unchanged frozen recovery program performs the actual inverse.

    Only systemctl is replaced inside a private mount/network namespace. No
    host service bus can be called, and package/inverse code runs from the
    capsule, not the checkout or a pre-imported monkeypatched module.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is required for private service-command isolation")
    receipt = stage_bootstrap(case)
    monkeypatch.setattr(activation, "_systemd", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    legacy.arm(case.paths, case.tx, receipt, receipt["preimages"],
               lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name))
    install(case, receipt)
    assert activation._watchdog_owns_current_armed_identity(case.paths, case.tx, receipt)
    receipt.update(status="package-installed", heartbeat_epoch=time.time() - 180,
                   database_before={"session_ids": ["preserved-session"]}, processes_before=[])
    activation._write(case.tx / "receipt.json", receipt)
    fake = case.tx / "fixture-systemctl"
    fake.write_text("#!/bin/sh\ncase \"$*\" in\n '--user disable termdeck-web-recovery.service'|'--user daemon-reload') exit 0;;\n '--user is-enabled termdeck-web-recovery.service') echo disabled; exit 1;;\n *) echo 'unexpected fixture service command' >&2; exit 99;;\nesac\n")
    fake.chmod(0o500)
    unit = (case.paths.unit_dir / activation.WATCHDOG_UNIT).read_text()
    writable, = (line.removeprefix("ReadWritePaths=").split()
                 for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    write_binds = [argument for path in writable for argument in ("--bind", path, path)]
    command = [bwrap, "--unshare-user", "--unshare-net", "--die-with-parent",
               "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               *write_binds,
               "--ro-bind", str(fake), "/usr/bin/systemctl", "--",
               sys.executable, "-I", "-B", str(case.tx / "legacy-recovery.py"), "--watchdog"]
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    final = activation._read(case.tx / "receipt.json")
    assert final["status"] == "rolled-back", result.stdout
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
