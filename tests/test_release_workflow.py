from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import termdeck.release_manager as release_manager
from termdeck import service_wrappers
from termdeck.release_manager import ReleaseError, install_release, rollback_release

from test_release_manager import (
    artifact,
    production_autostart_environment,
    target_activation_bundle,
    write_manifest,
    write_wheel,
)


_ROOT = Path(__file__).resolve().parents[1]
_RELEASE_WORKFLOW_SPEC = importlib.util.spec_from_file_location(
    "termdeck_check_release_workflow",
    _ROOT / "scripts" / "check_release_workflow.py",
)
assert _RELEASE_WORKFLOW_SPEC is not None
assert _RELEASE_WORKFLOW_SPEC.loader is not None
_RELEASE_WORKFLOW = importlib.util.module_from_spec(_RELEASE_WORKFLOW_SPEC)
_RELEASE_WORKFLOW_SPEC.loader.exec_module(_RELEASE_WORKFLOW)


def _preserved_path(path: Path) -> tuple[int, int, int, bytes | None]:
    entry = path.lstat()
    return (
        entry.st_dev,
        entry.st_ino,
        stat.S_IMODE(entry.st_mode),
        path.read_bytes() if stat.S_ISREG(entry.st_mode) else None,
    )


def _preserved_release_tree(path: Path) -> dict[Path, tuple[int, int, int, bytes | None]]:
    return {item: _preserved_path(item) for item in [path, *path.rglob("*")]}


def test_release_smoke_uses_clean_disposable_committed_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for args in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "release-workflow-test"],
        ["git", "config", "user.email", "release-workflow-test@invalid"],
    ):
        subprocess.run(args, cwd=repository, check=True)
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (repository / "deleted.txt").write_text("remove me\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (repository / "deleted.txt").unlink()
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    destination = tmp_path / "source"
    source_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    source_status_before = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    _RELEASE_WORKFLOW._clone_clean_source_snapshot(repository, destination)

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=destination,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    assert status.stdout == ""

    source_head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    source_status_after = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    assert source_head_after == source_head_before
    assert source_status_after == source_status_before

    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=destination,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    assert len(actual) == 40
    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "modified\n"
    assert not (destination / "deleted.txt").exists()
    assert (destination / "untracked.txt").read_text(encoding="utf-8") == "new\n"


@pytest.fixture()
def installed_release(tmp_path: Path) -> dict[str, object]:
    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy.write_bytes(b"#!/bin/sh\necho legacy\n")
    legacy.chmod(0o755)
    with mock.patch.object(release_manager, "_verify_staged_import"):
        result = install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    return {
        "wheel": wheel,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "root": root,
        "bin_dir": bin_dir,
        "result": result,
    }


def test_install_repairs_missing_wrapper_and_reports_no_restart(
    installed_release: dict[str, object],
) -> None:
    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    wheel = installed_release["wheel"]
    manifest_path = installed_release["manifest_path"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(wheel, Path)
    assert isinstance(manifest_path, Path)
    wrapper = root / "termdeck-web-backend"
    expected = wrapper.read_bytes()
    wrapper.unlink()

    with mock.patch.object(release_manager, "_verify_staged_import"):
        result = install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    assert wrapper.read_bytes() == expected
    assert result["repaired"] is True
    assert result["live_processes_restarted"] is False
    assert result["service_wrappers"]["repaired"] is True  # type: ignore[index]


def test_reinstall_preserves_release_tree_and_verification_receipt(
    installed_release: dict[str, object],
) -> None:
    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    manifest = installed_release["manifest"]
    assert isinstance(manifest, dict)
    release_id = str(manifest["release_id"])
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    release_dir = layout.releases_dir / release_id
    receipt = release_manager._release_verification_path(layout, release_id)
    before = _preserved_release_tree(release_dir)
    receipt_before = _preserved_path(receipt)

    with mock.patch.object(release_manager, "_verify_staged_import"):
        result = install_release(
            installed_release["wheel"],
            installed_release["manifest_path"],
            root=root,
            bin_dir=bin_dir,
        )

    assert result["idempotent"] is True
    assert _preserved_release_tree(release_dir) == before
    assert _preserved_path(receipt) == receipt_before
    assert release_manager.verify_installed_tree(release_dir)["ok"] is True


def test_failed_staging_preserves_existing_release_and_receipt(
    installed_release: dict[str, object],
) -> None:
    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    manifest = installed_release["manifest"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(manifest, dict)
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    release_id = str(manifest["release_id"])
    release_dir = layout.releases_dir / release_id
    receipt = release_manager._release_verification_path(layout, release_id)
    before = _preserved_release_tree(release_dir)
    receipt_before = _preserved_path(receipt)
    wheel_b, manifest_b, _ = artifact(root.parent, "b", version="0.2.13")
    verified_b = release_manager.verify_wheel(wheel_b, manifest_path=manifest_b)

    with mock.patch.object(
        release_manager,
        "_verify_installed_tree_details",
        side_effect=ReleaseError("staging verification unavailable"),
    ), pytest.raises(ReleaseError, match="staging verification unavailable"):
        release_manager._stage_release(layout, verified_b, manifest_b)

    assert not list(layout.releases_dir.glob(".staging-*"))
    assert _preserved_release_tree(release_dir) == before
    assert _preserved_path(receipt) == receipt_before


def test_anchored_capture_preserves_complete_regular_file_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "capture"
    target.write_bytes(b"authenticated publication bytes")
    target.chmod(0o640)
    missing = tmp_path / "absent"
    before = target.lstat()
    captured = release_manager._capture_anchored_files_safely(
        ((target, "positive capture"), (missing, "positive absent capture"))
    )
    assert captured[target].snapshot == release_manager._FileSnapshot(
        True, b"authenticated publication bytes", 0o640
    )
    assert captured[target].identity == release_manager._file_identity(before, label="test")
    assert captured[target].sha256 == release_manager.sha256_bytes(target.read_bytes())
    assert captured[missing].snapshot.exists is False
    assert captured[missing].identity is None


def test_current_release_wrappers_bind_verified_wheel_site(
    tmp_path: Path,
) -> None:
    """Current managed releases never execute wrapper policy from checkout."""

    version = "0.2.21"
    wheel = tmp_path / "immutable" / f"t1000-{version}-py3-none-any.whl"
    modules = {
        "termdeck/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "termdeck/service_wrappers.py": b"def main(**kwargs):\n    return 0\n",
        "termdeck/web_activation.py": b"# immutable activation module\n",
    }
    info = write_wheel(wheel, seed="a", version=version, extra_members=modules)
    manifest_path = tmp_path / "immutable" / "release.json"
    manifest = write_manifest(manifest_path, wheel, info)
    release_id = str(manifest["release_id"])
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    with mock.patch.object(release_manager, "_verify_staged_import"):
        result = install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    wrappers_result = result["service_wrappers"]
    assert isinstance(wrappers_result, dict)
    expected_site = root / "releases" / release_id / "site"
    assert wrappers_result["execution_root"] == str(expected_site)
    binding = wrappers_result["execution_binding"]
    assert isinstance(binding, dict)
    assert set(binding) == {
        "termdeck/__init__.py",
        "termdeck/service_wrappers.py",
        "termdeck/web_activation.py",
    }
    accessor = release_manager.release_wrapper_execution_binding(
        root=root,
        bin_dir=bin_dir,
        release_id=release_id,
    )
    expected_control_root = root.parent.with_name(root.parent.name + "-termdeck-control")
    assert accessor == {
        "release_id": release_id,
        "immutable": True,
        "execution_root": expected_site,
        "execution_binding": binding,
        "control_root": str(expected_control_root),
        "control_socket": str(expected_control_root / "runtime-attestd.sock"),
        "control_attestation": {
            "socket": str(expected_control_root / "runtime-attestd.sock"),
            "operation": service_wrappers.CONTROL_ATTEST_OPERATION,
            "request_schema": service_wrappers.CONTROL_ATTEST_REQUEST_SCHEMA,
            "response_schema": service_wrappers.CONTROL_ATTEST_RESPONSE_SCHEMA,
            "producer_principal": service_wrappers.CONTROL_ATTEST_PRODUCER,
            "excluded_principal": service_wrappers.CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
            "control_root_owner": service_wrappers.CONTROL_ATTEST_OWNER,
            "control_root_group": service_wrappers.CONTROL_ATTEST_GROUP,
            "control_root_mode": service_wrappers.CONTROL_ROOT_MODE,
            "socket_owner": service_wrappers.CONTROL_ATTEST_OWNER,
            "socket_group": service_wrappers.CONTROL_ATTEST_GROUP,
            "socket_mode": service_wrappers.CONTROL_ATTEST_SOCKET_MODE,
        },
    }
    assert wrappers_result["control_root"] == str(expected_control_root)
    assert wrappers_result["control_socket"] == str(expected_control_root / "runtime-attestd.sock")
    assert wrappers_result["control_attestation"] == accessor["control_attestation"]
    wrapper_manifest = json.loads((root / service_wrappers.WRAPPER_MANIFEST_NAME).read_text())
    assert wrapper_manifest["execution_root"] == str(expected_site)
    assert wrapper_manifest["execution_binding"] == binding


def test_immutable_wrapper_binding_tracks_rollback_target(tmp_path: Path) -> None:
    """B-to-A wrapper restoration renders against A's verified site, not B's."""

    version = "0.2.21"

    def current_artifact(seed: str) -> tuple[Path, Path, dict[str, object]]:
        wheel = tmp_path / seed / f"t1000-{version}-py3-none-any.whl"
        modules = {
            "termdeck/__init__.py": f'__version__ = "{version}"\n'.encode(),
            "termdeck/service_wrappers.py": (
                f"# selected wrapper module {seed}\n".encode()
            ),
            "termdeck/web_activation.py": (
                f"# selected activation module {seed}\n".encode()
            ),
        }
        info = write_wheel(wheel, seed=seed, version=version, extra_members=modules)
        manifest_path = tmp_path / seed / "release.json"
        return wheel, manifest_path, write_manifest(manifest_path, wheel, info)

    wheel_a, manifest_a_path, manifest_a = current_artifact("a")
    wheel_b, manifest_b_path, _manifest_b = current_artifact("b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    target_activation = target_activation_bundle(
        layout,
        Path(str(release_manager.load_managed_state(layout)["python_executable"])),
    )
    with mock.patch.object(
        release_manager,
        "_derive_release_activation_bundle",
        return_value=target_activation,
    ):
        rollback_release(root=root, bin_dir=bin_dir)

    expected_site = root / "releases" / str(manifest_a["release_id"]) / "site"
    wrapper_manifest = json.loads((root / service_wrappers.WRAPPER_MANIFEST_NAME).read_text())
    assert wrapper_manifest["execution_root"] == str(expected_site)
    readback = service_wrappers.readback_wrappers(
        **release_manager._wrapper_layout_kwargs(layout, str(manifest_a["release_id"])),
        expected_release_id=str(manifest_a["release_id"]),
    )
    assert readback["execution_root"] == str(expected_site)


def test_wrapper_execution_binding_rejects_control_contract_drift(
    installed_release: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public RM accessor cannot return unvalidated endpoint metadata."""

    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    manifest = installed_release["manifest"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(manifest, dict)
    release_id = manifest["release_id"]
    assert isinstance(release_id, str)

    monkeypatch.setattr(
        service_wrappers,
        "control_attestation_binding",
        lambda **_kwargs: {"socket": "drifted"},
    )
    with pytest.raises(ReleaseError, match="control attestation differs from policy"):
        release_manager.release_wrapper_execution_binding(
            root=root,
            bin_dir=bin_dir,
            release_id=release_id,
        )


def test_install_refuses_tampered_wrapper_destination(
    installed_release: dict[str, object],
) -> None:
    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    wheel = installed_release["wheel"]
    manifest_path = installed_release["manifest_path"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(wheel, Path)
    assert isinstance(manifest_path, Path)
    wrapper = root / "termdeck-web-backend"
    original = wrapper.read_bytes()
    wrapper.write_bytes(original + b"tampered")

    with mock.patch.object(release_manager, "_verify_staged_import"):
        with pytest.raises(ReleaseError, match="service-wrapper"):
            install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    assert wrapper.read_bytes() == original + b"tampered"


def test_first_install_interrupt_between_wrappers_and_state_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first install never leaves B wrappers without the fixed release journal."""

    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    journal = root / ".termdeck-publication-install.json"
    original_commit = release_manager._commit_staged_file
    interrupted = False

    def interrupt_before_state(
        parents: release_manager._PublicationParents,
        record: release_manager._PublicationRecord,
        **kwargs: object,
    ) -> release_manager._FileIdentity:
        nonlocal interrupted
        if record.label == "managed state":
            interrupted = True
            raise KeyboardInterrupt("simulate process death after wrapper publication")
        return original_commit(parents, record, **kwargs)

    def stop_in_process_rollback(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("simulate process death before local inverse")

    with mock.patch.object(release_manager, "_verify_staged_import"), monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_commit_staged_file", interrupt_before_state)
        scoped.setattr(release_manager, "_restore_record", stop_in_process_rollback)
        with pytest.raises(KeyboardInterrupt, match="process death"):
            install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    assert interrupted
    assert (root / service_wrappers.WRAPPER_MANIFEST_NAME).exists()
    assert not (root / "state.json").exists()
    assert journal.exists()

    recovered = release_manager.recover_install_publication(root=root, bin_dir=bin_dir)

    assert recovered["ok"] is True
    assert recovered["state"] == "rolled-back"
    assert not (root / service_wrappers.WRAPPER_MANIFEST_NAME).exists()
    assert not (root / "state.json").exists()
    assert not journal.exists()


def test_wrapper_manifest_is_bound_to_selected_release(
    installed_release: dict[str, object],
) -> None:
    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    manifest = installed_release["manifest"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(manifest, dict)
    wrapper_manifest = root / service_wrappers.WRAPPER_MANIFEST_NAME
    payload = json.loads(wrapper_manifest.read_text())
    payload["release_id"] = "tampered-release"
    wrapper_manifest.write_bytes(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    status = release_manager.managed_release_status(
        root=root,
        bin_dir=bin_dir,
        verify=True,
    )
    assert status["ok"] is False
    assert "service-wrapper" in str(status["error"])


def test_managed_rollback_restores_wrappers_and_does_not_restart_processes(
    tmp_path: Path,
) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy.write_bytes(b"#!/bin/sh\necho legacy\n")
    legacy.chmod(0o755)
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    target_activation = target_activation_bundle(
        layout,
        Path(str(release_manager.load_managed_state(layout)["python_executable"])),
    )

    with mock.patch.object(
        release_manager,
        "_derive_release_activation_bundle",
        return_value=target_activation,
    ):
        result = rollback_release(root=root, bin_dir=bin_dir)

    assert result["release_id"] == manifest_a["release_id"]
    assert result["live_processes_restarted"] is False
    wrapper_manifest = json.loads(
        (root / service_wrappers.WRAPPER_MANIFEST_NAME).read_text()
    )
    assert wrapper_manifest["release_id"] == manifest_a["release_id"]
    readback = service_wrappers.readback_wrappers(
        **release_manager._wrapper_layout_kwargs(
            release_manager.release_layout(root=root, bin_dir=bin_dir)
        ),
        expected_release_id=str(manifest_a["release_id"]),
    )
    assert readback["ok"] is True

    legacy_result = rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)
    assert legacy_result["target"] == "legacy-launcher"
    assert legacy_result["live_processes_restarted"] is False
    wrapper_layout = service_wrappers.resolve_layout(
        **release_manager._wrapper_layout_kwargs(
            release_manager.release_layout(root=root, bin_dir=bin_dir)
        )
    )
    assert not wrapper_layout.manifest_path.exists()
    assert all(
        not wrapper_layout.wrapper_path(spec, action).exists()
        for spec in service_wrappers.UNIT_SPECS
        for action in ("start", "stop")
    )
    assert all(
        not wrapper_layout.config_path(spec).exists()
        for spec in service_wrappers.UNIT_SPECS
    )


def test_inverse_recovery_uses_a_distinct_journal_from_retired_forward_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-dead B-to-A inverse cannot impersonate an old A-to-B journal.

    Activation retains the exact callback receipt from the normal B install
    even after release-manager removes its terminal journal.  The subsequent
    inverse must therefore publish its intent at a different fixed path; a
    retry first observes that the old path is absent, then recovers only the
    inverse's authenticated inode and may retry B-to-A normally.
    """

    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy.write_bytes(b"#!/bin/sh\necho legacy\n")
    legacy.chmod(0o755)
    forward_receipts: list[release_manager.PublicationTransactionReceipt] = []
    inverse_intents: list[release_manager.PublicationTransactionReceipt] = []
    recovery_receipts: list[release_manager.PublicationTransactionReceipt] = []

    def record_forward(
        receipt: release_manager.PublicationTransactionReceipt,
    ) -> None:
        forward_receipts.append(receipt)

    def record_inverse_intent(
        receipt: release_manager.PublicationTransactionReceipt,
    ) -> None:
        inverse_intents.append(receipt)

    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
            after_publication=record_forward,
        )

    assert len(forward_receipts) == 1
    forward = forward_receipts[0]
    assert forward.state == "published"
    assert forward.journal_path == root / ".termdeck-publication-install.json"
    assert not forward.journal_path.exists()

    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    target_activation = target_activation_bundle(
        layout,
        Path(str(release_manager.load_managed_state(layout)["python_executable"])),
    )
    original_commit = release_manager._commit_staged_file
    interrupted = False

    def interrupt_before_state(
        parents: release_manager._PublicationParents,
        record: release_manager._PublicationRecord,
        **kwargs: object,
    ) -> release_manager._FileIdentity:
        nonlocal interrupted
        if record.label == "managed state":
            interrupted = True
            raise KeyboardInterrupt("simulate hard death during inverse")
        return original_commit(parents, record, **kwargs)

    def stop_local_inverse(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt("simulate hard death before local inverse")

    with mock.patch.object(
        release_manager,
        "_derive_release_activation_bundle",
        return_value=target_activation,
    ), monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_commit_staged_file", interrupt_before_state)
        scoped.setattr(release_manager, "_restore_record", stop_local_inverse)
        with pytest.raises(KeyboardInterrupt, match="hard death"):
            rollback_release(
                root=root,
                bin_dir=bin_dir,
                before_publication=record_inverse_intent,
            )

    assert interrupted
    assert len(inverse_intents) == 1
    inverse = inverse_intents[0]
    assert inverse.state == "intent"
    assert inverse.journal_path == root / ".termdeck-publication-rollback.json"
    assert inverse.journal_path != forward.journal_path
    assert inverse.journal_path.exists()

    # This models the outer receipt's old package_publication record.  Its
    # retired forward pathname cannot resolve the fresh inverse inode.
    retired = release_manager.recover_install_publication(
        root=root,
        bin_dir=bin_dir,
        journal_path=forward.journal_path,
    )
    assert retired == {
        "ok": True,
        "recovered": False,
        "journal": str(forward.journal_path),
    }

    def capture_recovered(receipt: release_manager.PublicationTransactionReceipt) -> None:
        assert receipt.journal_path == inverse.journal_path
        assert inverse.journal_path.exists()
        recovery_receipts.append(receipt)

    recovered = release_manager.recover_install_publication(
        root=root,
        bin_dir=bin_dir,
        journal_path=inverse.journal_path,
        after_recovery=capture_recovered,
    )
    assert recovered["state"] == "rolled-back"
    assert len(recovery_receipts) == 1
    recovered_receipt = recovery_receipts[0]
    assert recovered_receipt.state == "recovered"
    assert recovered_receipt.journal_path == inverse.journal_path
    assert recovered["recovery_receipt"] == release_manager._publication_receipt_summary(
        recovered_receipt
    )
    # The receipt is the strict hand-off for activation: every B leaf was
    # captured through the still-live journal's anchored parents, including
    # its fresh identity when recovery had to recreate it.
    state_path = root / "state.json"
    state_postimage = recovered_receipt.postimages[state_path]
    assert state_postimage.identity is not None
    current_state = state_path.stat()
    assert state_postimage.identity.inode == current_state.st_ino
    assert state_postimage.identity.device == current_state.st_dev
    assert release_manager.load_managed_state(layout)["current_release_id"] == manifest_b["release_id"]
    assert not inverse.journal_path.exists()

    with mock.patch.object(
        release_manager,
        "_derive_release_activation_bundle",
        return_value=target_activation,
    ):
        retried = rollback_release(root=root, bin_dir=bin_dir)
    assert retried["release_id"] == manifest_a["release_id"]


def test_first_install_empty_legacy_rollback_restores_only_recorded_absence(
    tmp_path: Path,
) -> None:
    """A first install can return to an authenticated empty preimage."""

    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    wrapper_layout = service_wrappers.resolve_layout(
        **release_manager._wrapper_layout_kwargs(layout)
    )
    wrapper_paths = tuple(
        path
        for spec in service_wrappers.UNIT_SPECS
        for path in (
            wrapper_layout.wrapper_path(spec, "start"),
            wrapper_layout.wrapper_path(spec, "stop"),
            wrapper_layout.config_path(spec),
        )
    ) + (wrapper_layout.manifest_path,)
    first_receipt = release_manager._first_install_preimage_path(layout)
    wrapper_receipt = release_manager._legacy_wrapper_preimage_path(layout)
    sentinel = root / "unowned-sentinel"
    sentinel.write_bytes(b"leave this outside the transaction")

    assert all(path.exists() for path in (*wrapper_paths, layout.bootstrap_path, layout.state_path, layout.launcher_path))
    assert first_receipt.exists()
    assert wrapper_receipt.exists()
    receipt = json.loads(first_receipt.read_text())
    assert receipt["activation_preimage"] == [
        {"path": str(layout.bootstrap_path), "exists": False},
        {"path": str(layout.state_path), "exists": False},
        {"path": str(layout.launcher_path), "exists": False},
    ]
    assert receipt["receipt_preimage"] == [
        {"path": str(wrapper_receipt), "exists": False},
        {"path": str(first_receipt), "exists": False},
    ]
    result = rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert result["target"] == "legacy-empty"
    assert result["previous_release_id"] == manifest["release_id"]
    assert result["live_processes_restarted"] is False
    assert not layout.bootstrap_path.exists()
    assert not layout.state_path.exists()
    assert not layout.launcher_path.exists()
    assert not first_receipt.exists()
    assert not wrapper_receipt.exists()
    assert all(not path.exists() for path in wrapper_paths)
    assert release_manager.load_managed_state(layout) == release_manager._default_state()
    assert sentinel.read_bytes() == b"leave this outside the transaction"


@pytest.mark.parametrize("tamper", ("launcher", "receipt"))
def test_first_install_empty_legacy_rollback_rejects_tampered_receipt_or_b(
    tmp_path: Path,
    tamper: str,
) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    if tamper == "launcher":
        layout.launcher_path.write_bytes(b"#!/bin/sh\necho tampered\n")
    else:
        release_manager._first_install_preimage_path(layout).unlink()

    with pytest.raises(ReleaseError):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert layout.state_path.exists()
    assert layout.bootstrap_path.exists()
    assert layout.launcher_path.exists()


def test_first_install_empty_legacy_rollback_rejects_same_bytes_identity_swap(
    tmp_path: Path,
) -> None:
    """The empty receipt authorizes the verified B instance, not just its bytes."""

    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    original = layout.launcher_path.read_bytes()
    replacement = tmp_path / "same-launcher-bytes"
    replacement.write_bytes(original)
    replacement.chmod(layout.launcher_path.stat().st_mode & 0o7777)
    real_publish = release_manager._publish_files_transactionally
    swapped = False

    def replace_after_authentication(writes, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            replacement.replace(layout.launcher_path)
            swapped = True
        return real_publish(writes, *args, **kwargs)

    with mock.patch.object(
        release_manager,
        "_publish_files_transactionally",
        side_effect=replace_after_authentication,
    ):
        with pytest.raises(ReleaseError, match="empty legacy rollback t1000 changed after preflight"):
            rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert swapped is True
    assert layout.state_path.exists()
    assert layout.bootstrap_path.exists()
    assert layout.launcher_path.read_bytes() == original


def test_first_install_empty_legacy_rollback_restores_foreign_wrapper_leaf_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authenticated empty rollback does not unlink an intervening wrapper leaf."""

    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    target = root / service_wrappers.WRAPPER_NAMES[0]
    outside = tmp_path / "outside-first-install-wrapper"
    outside.write_bytes(b"foreign first-install rollback leaf")
    outside.chmod(0o755)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
            and label.startswith("empty legacy rollback")
        ):
            injected = True
            outside.replace(target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(ReleaseError, match="changed before removal"):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert injected
    assert target.read_bytes() == b"foreign first-install rollback leaf"
    assert not outside.exists()
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    assert layout.state_path.exists()
    assert layout.bootstrap_path.exists()
    assert layout.launcher_path.exists()


def test_production_first_install_empty_legacy_rollback_removes_authenticated_autostart(
    tmp_path: Path,
) -> None:
    """Canonical first-install rollback also removes its absent-baseline desktop pair."""

    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    descriptor = tmp_path / "config" / "autostart" / "io.github.t1000.T1000.desktop"

    with production_autostart_environment(
        bin_dir=bin_dir,
        descriptor_path=descriptor,
    ), mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(
            wheel,
            manifest_path,
            root=root,
            bin_dir=bin_dir,
            production=True,
        )
        layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
        autostart_receipt = root / release_manager._AUTOSTART_RECEIPT_NAME
        first_receipt = release_manager._first_install_preimage_path(layout)
        receipt = json.loads(first_receipt.read_text())
        assert receipt["autostart_preimage"] == [
            {"path": str(descriptor), "exists": False},
            {"path": str(autostart_receipt), "exists": False},
        ]
        assert descriptor.exists()
        assert autostart_receipt.exists()

        result = rollback_release(
            root=root,
            bin_dir=bin_dir,
            production=True,
            to_legacy=True,
        )

    assert result["target"] == "legacy-empty"
    assert not descriptor.exists()
    assert not autostart_receipt.exists()
    assert not layout.state_path.exists()
    assert not layout.bootstrap_path.exists()
    assert not layout.launcher_path.exists()


def test_failed_activation_restores_wrappers(
    tmp_path: Path,
) -> None:
    wheel_a, manifest_a_path, _ = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)
    layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
    target_activation = target_activation_bundle(
        layout,
        Path(str(release_manager.load_managed_state(layout)["python_executable"])),
    )
    wrapper_manifest = root / service_wrappers.WRAPPER_MANIFEST_NAME
    before = wrapper_manifest.read_bytes()

    with mock.patch.object(
        release_manager,
        "_derive_release_activation_bundle",
        return_value=target_activation,
    ), mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        side_effect=ReleaseError("forced activation failure"),
    ):
        with pytest.raises(ReleaseError, match="activation publication"):
            rollback_release(root=root, bin_dir=bin_dir)

    assert wrapper_manifest.read_bytes() == before
    assert json.loads(before)["release_id"] == manifest_b["release_id"]


def test_legacy_rollback_rejects_replaced_wrapper_parent(
    installed_release: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollback preimage guard survives the prepare/publish handoff."""

    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    outside = root.with_name("outside-rollback")
    outside.mkdir()
    sentinel = outside / service_wrappers.WRAPPER_NAMES[0]
    sentinel.write_bytes(b"outside rollback sentinel")
    detached = root.with_name("managed-before-rollback")
    original_renameat2 = release_manager._renameat2
    swapped = False

    def swap_then_commit(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal swapped
        if not swapped and source.startswith(".") and ".tmp-" in source:
            swapped = True
            root.rename(detached)
            root.symlink_to(outside, target_is_directory=True)
        original_renameat2(
            descriptor,
            source,
            destination,
            flags=flags,
            label=label,
        )

    monkeypatch.setattr(release_manager, "_renameat2", swap_then_commit)
    with pytest.raises(ReleaseError, match="parent"):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert swapped
    assert sentinel.read_bytes() == b"outside rollback sentinel"


def test_legacy_rollback_keyboard_interrupt_keeps_one_recoverable_journal(
    installed_release: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy wrappers cannot become an unowned generation before state moves."""

    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    manifest = installed_release["manifest"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    assert isinstance(manifest, dict)
    current = str(manifest["release_id"])
    journal = root / ".termdeck-publication-rollback.json"
    original_commit = release_manager._commit_staged_file
    interrupted = False

    def interrupt_before_launcher(
        parents: release_manager._PublicationParents,
        record: release_manager._PublicationRecord,
        **kwargs: object,
    ) -> release_manager._FileIdentity:
        nonlocal interrupted
        if record.label == "t1000 launcher":
            interrupted = True
            raise KeyboardInterrupt("simulate process death after legacy wrappers")
        return original_commit(parents, record, **kwargs)

    def stop_in_process_rollback(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("simulate process death during in-process rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_commit_staged_file", interrupt_before_launcher)
        scoped.setattr(release_manager, "_restore_record", stop_in_process_rollback)
        with pytest.raises(KeyboardInterrupt, match="process death"):
            rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert interrupted
    assert release_manager.load_managed_state(
        release_manager.release_layout(root=root, bin_dir=bin_dir)
    )["current_release_id"] == current
    # The legacy inverse started, but the fixed release journal remains the
    # only ownership record until a new process restores B's exact tombstones.
    assert not (root / service_wrappers.WRAPPER_MANIFEST_NAME).exists()
    assert journal.exists()

    recovered = release_manager.recover_install_publication(root=root, bin_dir=bin_dir)

    assert recovered["ok"] is True
    assert recovered["state"] == "rolled-back"
    assert release_manager.load_managed_state(
        release_manager.release_layout(root=root, bin_dir=bin_dir)
    )["current_release_id"] == current
    readback = service_wrappers.readback_wrappers(
        **release_manager._wrapper_layout_kwargs(
            release_manager.release_layout(root=root, bin_dir=bin_dir)
        ),
        expected_release_id=current,
    )
    assert readback["ok"] is True
    assert not journal.exists()


def test_legacy_rollback_foreign_wrapper_survives_combined_compensation(
    installed_release: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed legacy inverse never recaptures a foreign wrapper as B."""

    root = installed_release["root"]
    bin_dir = installed_release["bin_dir"]
    assert isinstance(root, Path)
    assert isinstance(bin_dir, Path)
    target = root / service_wrappers.WRAPPER_NAMES[0]
    outside = root.parent / "foreign-legacy-wrapper"
    journal = root / ".termdeck-publication-rollback.json"
    injected = False

    def inject_foreign_then_fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal injected
        if not injected:
            injected = True
            outside.write_bytes(b"foreign legacy wrapper")
            outside.chmod(0o755)
            outside.replace(target)
        raise ReleaseError("forced legacy activation verification failure")

    monkeypatch.setattr(
        release_manager,
        "_verify_managed_activation",
        inject_foreign_then_fail,
    )
    with pytest.raises(ReleaseError, match="rollback was incomplete"):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert injected
    assert target.read_bytes() == b"foreign legacy wrapper"
    assert not outside.exists()
    assert journal.exists()
    with pytest.raises(ReleaseError, match="foreign canonical"):
        release_manager.recover_install_publication(root=root, bin_dir=bin_dir)
    assert target.read_bytes() == b"foreign legacy wrapper"
    assert journal.exists()


@pytest.mark.parametrize("to_legacy", [False, True])
def test_failed_production_rollback_preserves_autostart_and_release_identity(
    tmp_path: Path, to_legacy: bool,
) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "t1000"
    launcher.write_bytes(b"#!/bin/sh\necho legacy\n")
    launcher.chmod(0o755)
    descriptor = tmp_path / "config" / "autostart" / "io.github.t1000.T1000.desktop"

    def reject_handoff(_receipt: release_manager.PublicationTransactionReceipt) -> None:
        raise RuntimeError("activation handoff unavailable")

    with production_autostart_environment(
        bin_dir=bin_dir, descriptor_path=descriptor,
    ), mock.patch.object(release_manager, "_verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir, production=True)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir, production=True)
        layout = release_manager.release_layout(root=root, bin_dir=bin_dir)
        selected = release_manager.load_managed_state(layout)
        autostart_receipt = root / release_manager._AUTOSTART_RECEIPT_NAME
        paths = [
            layout.state_path, layout.bootstrap_path, layout.launcher_path,
            descriptor, autostart_receipt,
            *(root / name for name in (*service_wrappers.WRAPPER_NAMES, service_wrappers.WRAPPER_MANIFEST_NAME)),
        ]
        before = {path: _preserved_path(path) for path in paths}
        release_paths = [
            layout.releases_dir / str(item["release_id"])
            for item in (manifest_a, manifest_b)
        ]
        trees = {path: _preserved_release_tree(path) for path in release_paths}
        receipts = {
            release_manager._release_verification_path(layout, str(item["release_id"])):
            _preserved_path(release_manager._release_verification_path(layout, str(item["release_id"])))
            for item in (manifest_a, manifest_b)
        }
        activation = target_activation_bundle(layout, Path(str(selected["python_executable"])))
        recovered: list[release_manager.PublicationTransactionReceipt] = []

        def record_recovery(receipt: release_manager.PublicationTransactionReceipt) -> None:
            assert receipt.state == "recovered"
            assert receipt.journal_path.exists()
            for path, snapshot in receipt.postimages.items():
                if snapshot.snapshot.exists:
                    assert snapshot.identity == release_manager._file_identity(path.lstat(), label="recovered")
            recovered.append(receipt)

        with mock.patch.object(
            release_manager, "_derive_release_activation_bundle", return_value=activation,
        ), pytest.raises(ReleaseError, match="activation handoff unavailable"):
            rollback_release(
                root=root, bin_dir=bin_dir, production=True,
                to_legacy=to_legacy, after_publication=reject_handoff,
                after_recovery=record_recovery,
            )

        assert len(recovered) == 1
        assert {path: _preserved_path(path) for path in paths} == before
        assert {path: _preserved_release_tree(path) for path in release_paths} == trees
        assert {path: _preserved_path(path) for path in receipts} == receipts
        assert release_manager._verify_production_autostart(layout, selected)["status"] == "managed"
        assert not release_manager._rollback_publication_journal_path(layout).exists()


def test_first_install_inverse_resumes_discarded_absent_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "first-publication"
    journal = tmp_path / ".termdeck-publication-first-inverse.json"
    original_phase = release_manager._journal_record_phase
    interrupted = False

    def record_then_interrupt(journal_state, record, phase: str) -> None:
        nonlocal interrupted
        original_phase(journal_state, record, phase)
        if phase == "rollback-discarded" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("interrupted inverse checkpoint")

    def unavailable_verifier() -> None:
        raise RuntimeError("publication verification unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_journal_record_phase", record_then_interrupt)
        with pytest.raises(KeyboardInterrupt, match="interrupted inverse checkpoint"):
            release_manager._publish_files_transactionally(
                ((target, b"installed bytes", 0o640, "first publication"),),
                verify=unavailable_verifier, journal_path=journal,
            )

    assert interrupted
    assert not target.exists()
    assert journal.exists()
    result = release_manager.recover_publication_transaction(journal)
    assert result["state"] == "rolled-back"
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_terminal_journal_disposal_resumes_from_original_recovery_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "published"
    journal = tmp_path / ".termdeck-publication-terminal.json"
    with mock.patch.object(release_manager._PublicationJournal, "finalize_terminal", return_value=False):
        release_manager._publish_files_transactionally(
            ((target, b"durable publication", 0o640, "terminal publication"),),
            journal_path=journal,
        )
    target_before = _preserved_path(target)
    identity = release_manager._file_identity(journal.lstat(), label="terminal journal")
    disposal_name = release_manager._terminal_journal_disposal_name(journal.name, identity)
    anchor = release_manager._open_directory_anchor(
        tmp_path, label="terminal journal fixture", create_parents=False,
    )
    try:
        release_manager._renameat2(
            anchor.descriptor, journal.name, disposal_name,
            flags=release_manager._RENAME_NOREPLACE, label="terminal fixture checkpoint",
        )
        anchor.fsync(label="terminal fixture checkpoint")
    finally:
        anchor.close()
    assert not journal.exists()
    assert (tmp_path / disposal_name).stat().st_ino == identity.inode

    result = release_manager.recover_publication_transaction(journal)

    assert result["state"] == "committed-cleaned"
    assert _preserved_path(target) == target_before
    assert list(tmp_path.iterdir()) == [target]


def test_local_recovery_retains_journal_until_fresh_identity_handoff(
    tmp_path: Path,
) -> None:
    target = tmp_path / "activation"
    target.write_bytes(b"previous activation")
    target.chmod(0o640)
    before = _preserved_path(target)
    journal = tmp_path / ".termdeck-publication-local-recovery.json"
    recovered: list[release_manager.PublicationTransactionReceipt] = []

    def reject_publication(_receipt: release_manager.PublicationTransactionReceipt) -> None:
        raise RuntimeError("outer publication unavailable")

    def defer_recovery(receipt: release_manager.PublicationTransactionReceipt) -> None:
        assert receipt.state == "recovered"
        assert receipt.journal_path == journal
        assert journal.exists()
        assert receipt.postimages[target].identity == release_manager._file_identity(
            target.lstat(), label="restored activation",
        )
        recovered.append(receipt)
        raise RuntimeError("outer receipt storage unavailable")

    with pytest.raises(ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"proposed activation", 0o600, "local recovery"),),
            journal_path=journal, after_publish=reject_publication,
            after_recovery=defer_recovery,
        )

    assert len(recovered) == 1
    assert _preserved_path(target) == before
    assert journal.exists()
    result = release_manager.recover_publication_transaction(
        journal, after_recovery=recovered.append,
    )
    assert result["state"] == "rolled-back"
    assert len(recovered) == 2
    assert _preserved_path(target) == before
    assert list(tmp_path.iterdir()) == [target]
