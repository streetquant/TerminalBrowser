from __future__ import annotations

import base64
from contextlib import nullcontext
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from termdeck.build_info import (
    BUILD_INFO_SCHEMA,
    MANAGED_STATE_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    canonical_json_bytes,
    compute_build_id,
    compute_release_id,
    sha256_bytes,
    sha256_file,
)
from termdeck.release_manager import (
    ReleaseError,
    _atomic_write_bytes,
    _copy_snapshot,
    install_release,
    load_managed_state,
    managed_release_status,
    release_layout,
    release_lock,
    rollback_release,
    semantic_runtime_snapshot,
    snapshot_source,
    validate_managed_state,
    verify_installed_tree,
    verify_wheel,
)
from termdeck.session_store import SessionStore


VERSION = "0.2.10"
DIST_INFO = f"t1000-{VERSION}.dist-info"
WHEEL_NAME = f"t1000-{VERSION}-py3-none-any.whl"


def build_info(seed: str) -> dict[str, object]:
    info: dict[str, object] = {
        "schema": BUILD_INFO_SCHEMA,
        "version": VERSION,
        "source_commit": seed * 40,
        "source_tree_sha256": seed * 64,
        "source_date_epoch": 1_700_000_000,
        "build_mode": "release",
    }
    info["build_id"] = compute_build_id(info)
    return info


def record_hash(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(content).digest()
    ).rstrip(b"=").decode("ascii")


def _zip_info(name: str, *, mode: int = stat.S_IFREG | 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def write_wheel(
    path: Path,
    *,
    seed: str = "a",
    extra_members: dict[str, bytes] | None = None,
    special_modes: dict[str, int] | None = None,
    duplicate: str | None = None,
    omit_record: str | None = None,
    extra_record: str | None = None,
    bad_hash: str | None = None,
    bad_size: str | None = None,
    entry_point: str = "t1000 = termdeck.cli:main\n",
    dist_info: str = DIST_INFO,
    wheel_metadata: str | None = None,
) -> dict[str, object]:
    info = build_info(seed)
    members: dict[str, bytes] = {
        "termdeck/__init__.py": f'__version__ = "{VERSION}"\n'.encode(),
        "termdeck/cli.py": b"def main():\n    return 0\n",
        "termdeck/_build_info.json": canonical_json_bytes(info),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: t1000\nVersion: {VERSION}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            wheel_metadata
            or (
                "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            )
        ).encode(),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\n" + entry_point
        ).encode(),
    }
    members.update(extra_members or {})
    record_name = f"{dist_info}/RECORD"
    rows: list[tuple[str, str, str]] = []
    for name, content in sorted(members.items()):
        if name == omit_record:
            continue
        digest = "sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" if name == bad_hash else record_hash(content)
        size = str(len(content) + 1) if name == bad_size else str(len(content))
        rows.append((name, digest, size))
    if extra_record:
        rows.append((extra_record, record_hash(b"missing"), str(len(b"missing"))))
    rows.append((record_name, "", ""))
    record_stream = io.StringIO(newline="")
    csv.writer(record_stream, lineterminator="\n").writerows(rows)
    members[record_name] = record_stream.getvalue().encode()

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            mode = (special_modes or {}).get(name, stat.S_IFREG | 0o644)
            archive.writestr(_zip_info(name, mode=mode), content)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(_zip_info(duplicate), members[duplicate])
    return info


def write_manifest(path: Path, wheel: Path, info: dict[str, object]) -> dict[str, object]:
    wheel_hash = sha256_file(wheel)
    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "release_id": compute_release_id(str(info["build_id"]), wheel_hash),
        "build_info": info,
        "wheel": wheel.name,
        "wheel_sha256": wheel_hash,
        "wheel_size": wheel.stat().st_size,
    }
    path.write_bytes(canonical_json_bytes(manifest))
    return manifest


def artifact(tmp_path: Path, seed: str) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / seed
    wheel = root / WHEEL_NAME
    info = write_wheel(wheel, seed=seed)
    manifest_path = root / f"{wheel.stem}.release.json"
    manifest = write_manifest(manifest_path, wheel, info)
    return wheel, manifest_path, manifest


def init_git_project(root: Path) -> None:
    (root / "termdeck").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='t1000'\nversion='0.2.10'\n",
        encoding="utf-8",
    )
    (root / "termdeck" / "__init__.py").write_text(
        '__version__ = "0.2.10"\n', encoding="utf-8"
    )
    (root / "termdeck" / "_build_info.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)


def test_snapshot_source_refuses_dirty_by_default_and_labels_candidate(tmp_path: Path) -> None:
    init_git_project(tmp_path)
    clean = snapshot_source(tmp_path)
    assert clean.build_mode == "release"
    assert clean.source_commit and "+dirty" not in clean.source_commit
    assert len(clean.source_tree_sha256) == 64

    (tmp_path / "termdeck" / "__init__.py").write_text(
        '__version__ = "0.2.10"\n# dirty\n', encoding="utf-8"
    )
    with pytest.raises(ReleaseError, match="clean worktree"):
        snapshot_source(tmp_path)
    dirty = snapshot_source(tmp_path, allow_dirty=True)
    assert dirty.build_mode == "dirty-candidate"
    assert dirty.source_commit.endswith("+dirty")
    assert dirty.dirty_entries


def test_snapshot_source_rejects_tracked_symlink(tmp_path: Path) -> None:
    init_git_project(tmp_path)
    (tmp_path / "linked").symlink_to("pyproject.toml")
    subprocess.run(["git", "add", "linked"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "link"], cwd=tmp_path, check=True)
    with pytest.raises(ReleaseError, match="symbolic link"):
        snapshot_source(tmp_path)


def test_snapshot_root_and_export_are_bound_to_the_original_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    init_git_project(source)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ReleaseError, match="source root.*symlink"):
        snapshot_source(alias)

    snapshot = snapshot_source(source)
    (source / "termdeck" / "__init__.py").write_text(
        '__version__ = "0.2.10"\n# changed after receipt\n', encoding="utf-8"
    )
    with pytest.raises(ReleaseError, match="exported tree|source changed"):
        _copy_snapshot(snapshot, tmp_path / "export")


def test_verify_valid_wheel_and_manifest(tmp_path: Path) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    verified = verify_wheel(wheel, manifest_path=manifest_path)
    assert verified.sha256 == manifest["wheel_sha256"]
    assert verified.build_info == manifest["build_info"]
    assert verified.distribution == "t1000"
    assert f"{DIST_INFO}/RECORD" in verified.members


def test_verify_rejects_symlinked_wheel_and_manifest_inputs(tmp_path: Path) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    wheel_link = tmp_path / "wheel-link" / WHEEL_NAME
    wheel_link.parent.mkdir()
    wheel_link.symlink_to(wheel)
    with pytest.raises(ReleaseError, match="open wheel artifact safely"):
        verify_wheel(wheel_link)

    manifest_link = tmp_path / "manifest-link.release.json"
    manifest_link.symlink_to(manifest_path)
    with pytest.raises(ReleaseError, match="open release manifest safely"):
        verify_wheel(wheel, manifest_path=manifest_link)


def test_verify_rejects_filename_dist_info_and_top_level_mismatch(tmp_path: Path) -> None:
    renamed = tmp_path / "renamed.whl"
    write_wheel(renamed)
    with pytest.raises(ReleaseError, match="filename"):
        verify_wheel(renamed)

    wrong_dist = tmp_path / WHEEL_NAME
    write_wheel(wrong_dist, dist_info="t1000-9.9.dist-info")
    with pytest.raises(ReleaseError, match="dist-info"):
        verify_wheel(wrong_dist)

    unexpected = tmp_path / "unexpected" / WHEEL_NAME
    write_wheel(unexpected, extra_members={"payload.txt": b"outside package"})
    with pytest.raises(ReleaseError, match="unexpected top-level"):
        verify_wheel(unexpected)


@pytest.mark.parametrize(
    "wheel_metadata, message",
    [
        (
            "Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            "Wheel-Version",
        ),
        (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
            "Root-Is-Purelib",
        ),
        (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp314-cp314-linux_x86_64\n",
            "Tag",
        ),
        (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\nTag: py2-none-any\n",
            "Tag",
        ),
    ],
)
def test_verify_rejects_unsupported_wheel_metadata(
    tmp_path: Path, wheel_metadata: str, message: str
) -> None:
    wheel = tmp_path / WHEEL_NAME
    write_wheel(wheel, wheel_metadata=wheel_metadata)
    with pytest.raises(ReleaseError, match=message):
        verify_wheel(wheel)


def test_verify_rejects_extra_entrypoint_and_package_version_mismatch(tmp_path: Path) -> None:
    wheel = tmp_path / WHEEL_NAME
    write_wheel(
        wheel,
        entry_point=(
            "t1000 = termdeck.cli:main\n"
            "unexpected = termdeck.cli:main\n"
        ),
    )
    with pytest.raises(ReleaseError, match="exactly"):
        verify_wheel(wheel)

    mismatched = tmp_path / "version" / WHEEL_NAME
    write_wheel(
        mismatched,
        extra_members={"termdeck/__init__.py": b'__version__ = "9.9"\n'},
    )
    with pytest.raises(ReleaseError, match="__version__"):
        verify_wheel(mismatched)


@pytest.mark.parametrize(
    "member",
    ["../escape.py", "/absolute.py", "termdeck\\escape.py", "termdeck//escape.py"],
)
def test_verify_rejects_unsafe_wheel_paths(tmp_path: Path, member: str) -> None:
    wheel = tmp_path / WHEEL_NAME
    write_wheel(wheel, extra_members={member: b"payload"})
    with pytest.raises(ReleaseError, match="unsafe|canonical"):
        verify_wheel(wheel)


def test_verify_rejects_symlink_and_duplicate_members(tmp_path: Path) -> None:
    symlink = tmp_path / "symlink.whl"
    write_wheel(
        symlink,
        extra_members={"termdeck/link.py": b"target"},
        special_modes={"termdeck/link.py": stat.S_IFLNK | 0o777},
    )
    with pytest.raises(ReleaseError, match="symlink|special"):
        verify_wheel(symlink)

    duplicate = tmp_path / "duplicate.whl"
    write_wheel(duplicate, duplicate="termdeck/cli.py")
    with pytest.raises(ReleaseError, match="duplicate"):
        verify_wheel(duplicate)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"omit_record": "termdeck/cli.py"}, "coverage"),
        ({"extra_record": "missing.py"}, "coverage"),
        ({"bad_hash": "termdeck/cli.py"}, "hash mismatch"),
        ({"bad_size": "termdeck/cli.py"}, "size mismatch"),
        ({"entry_point": "other = termdeck.cli:main\n"}, "entry_points"),
    ],
)
def test_verify_rejects_record_and_entrypoint_tamper(
    tmp_path: Path, kwargs: dict[str, str], message: str
) -> None:
    wheel = tmp_path / WHEEL_NAME
    write_wheel(wheel, **kwargs)
    with pytest.raises(ReleaseError, match=message):
        verify_wheel(wheel)


def test_verify_rejects_noncanonical_or_mismatched_manifest(tmp_path: Path) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "b")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ReleaseError, match="canonical"):
        verify_wheel(wheel, manifest_path=manifest_path)

    manifest_path.write_bytes(canonical_json_bytes(manifest))
    wrong = dict(manifest)
    wrong["wheel_sha256"] = "0" * 64
    wrong["release_id"] = compute_release_id(
        str(wrong["build_info"]["build_id"]), str(wrong["wheel_sha256"])
    )
    manifest_path.write_bytes(canonical_json_bytes(wrong))
    with pytest.raises(ReleaseError, match="hash or size"):
        verify_wheel(wheel, manifest_path=manifest_path)


def test_managed_state_validation_is_closed_and_generation_bound() -> None:
    valid = {
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
    assert validate_managed_state(valid) == dict(sorted(valid.items()))
    for mutation in (
        {"generation": -1},
        {"current_release_id": "bad"},
        {"manifest_sha256": "0" * 64},
        {"legacy_launcher": {"path": "x"}},
        {"unexpected": True},
    ):
        candidate = dict(valid)
        candidate.update(mutation)
        with pytest.raises(ReleaseError):
            validate_managed_state(candidate)


def test_atomic_install_upgrade_rollback_idempotence_and_legacy_restore(tmp_path: Path) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = b"#!/bin/sh\necho legacy\n"
    (bin_dir / "t1000").write_bytes(legacy)
    (bin_dir / "t1000").chmod(0o755)

    with mock.patch("termdeck.release_manager._verify_staged_import"):
        first = install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        second = install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)

    assert first["release_id"] == manifest_a["release_id"]
    assert second["release_id"] == manifest_b["release_id"]
    layout = release_layout(root=root, bin_dir=bin_dir)
    state = load_managed_state(layout)
    assert state["current_release_id"] == manifest_b["release_id"]
    assert state["previous_release_id"] == manifest_a["release_id"]
    assert verify_installed_tree(root / "releases" / str(manifest_a["release_id"]))["ok"]
    assert verify_installed_tree(root / "releases" / str(manifest_b["release_id"]))["ok"]

    rolled = rollback_release(root=root, bin_dir=bin_dir)
    assert rolled["release_id"] == manifest_a["release_id"]
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        idempotent = install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
    assert idempotent["idempotent"] is True

    legacy_result = rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)
    assert legacy_result["target"] == "legacy-launcher"
    assert (bin_dir / "t1000").read_bytes() == legacy


def test_install_refuses_external_launcher_change(tmp_path: Path) -> None:
    wheel_a, manifest_a_path, _ = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, _ = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "t1000").write_text("legacy\n", encoding="utf-8")
    (bin_dir / "t1000").chmod(0o755)
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
    (bin_dir / "t1000").write_text("tampered\n", encoding="utf-8")
    with mock.patch("termdeck.release_manager._verify_staged_import"), pytest.raises(
        ReleaseError, match="changed outside"
    ):
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)


def test_interrupted_state_publication_keeps_previous_release_selected(tmp_path: Path) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, _ = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state_before = layout.state_path.read_bytes()
    bootstrap_before = layout.bootstrap_path.read_bytes()
    launcher_before = layout.launcher_path.read_bytes()
    real_atomic = _atomic_write_bytes
    failed = False

    def fail_state(path: Path, content: bytes, *, mode: int = 0o644) -> None:
        nonlocal failed
        if Path(path) == layout.state_path and not failed:
            failed = True
            raise OSError("simulated fsync failure")
        real_atomic(Path(path), content, mode=mode)

    with mock.patch("termdeck.release_manager._verify_staged_import"), mock.patch(
        "termdeck.release_manager._atomic_write_bytes", side_effect=fail_state
    ), pytest.raises(ReleaseError, match="rolled back"):
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)

    assert layout.state_path.read_bytes() == state_before
    assert layout.bootstrap_path.read_bytes() == bootstrap_before
    assert layout.launcher_path.read_bytes() == launcher_before
    assert load_managed_state(layout)["current_release_id"] == manifest_a["release_id"]


@pytest.mark.parametrize("failure_target", ["bootstrap", "state", "launcher", "verify"])
def test_first_install_publication_failure_restores_every_preimage(
    tmp_path: Path,
    failure_target: str,
) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy_bytes = b"#!/bin/sh\necho legacy\n"
    legacy.write_bytes(legacy_bytes)
    legacy.chmod(0o755)
    layout = release_layout(root=root, bin_dir=bin_dir)
    real_atomic = _atomic_write_bytes
    failed = False
    target_path = {
        "bootstrap": layout.bootstrap_path,
        "state": layout.state_path,
        "launcher": layout.launcher_path,
    }.get(failure_target)

    def fail_once(path: Path, content: bytes, *, mode: int = 0o644) -> None:
        nonlocal failed
        if target_path is not None and Path(path) == target_path and not failed:
            failed = True
            raise OSError(f"simulated {failure_target} publication failure")
        real_atomic(Path(path), content, mode=mode)

    patches = [
        mock.patch("termdeck.release_manager._verify_staged_import"),
        mock.patch("termdeck.release_manager._atomic_write_bytes", side_effect=fail_once),
    ]
    if failure_target == "verify":
        patches.append(
            mock.patch(
                "termdeck.release_manager._verify_managed_activation",
                side_effect=ReleaseError("simulated post-publication verification failure"),
            )
        )
    with patches[0], patches[1]:
        verification = patches[2] if len(patches) == 3 else nullcontext()
        with verification, pytest.raises(ReleaseError, match="rolled back"):
            install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    assert legacy.read_bytes() == legacy_bytes
    assert legacy.stat().st_mode & 0o777 == 0o755
    assert not layout.state_path.exists()
    assert not layout.bootstrap_path.exists()


@pytest.mark.parametrize("failure_target", ["launcher", "state", "verify"])
def test_legacy_rollback_failure_restores_managed_activation(
    tmp_path: Path,
    failure_target: str,
) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy.write_bytes(b"#!/bin/sh\necho legacy\n")
    legacy.chmod(0o755)
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state_before = layout.state_path.read_bytes()
    launcher_before = layout.launcher_path.read_bytes()
    real_atomic = _atomic_write_bytes
    failed = False
    target_path = {
        "launcher": layout.launcher_path,
        "state": layout.state_path,
    }.get(failure_target)

    def fail_once(path: Path, content: bytes, *, mode: int = 0o644) -> None:
        nonlocal failed
        if target_path is not None and Path(path) == target_path and not failed:
            failed = True
            raise OSError(f"simulated rollback {failure_target} failure")
        real_atomic(Path(path), content, mode=mode)

    verification = (
        mock.patch(
            "termdeck.release_manager._verify_managed_activation",
            side_effect=ReleaseError("simulated rollback verification failure"),
        )
        if failure_target == "verify"
        else nullcontext()
    )
    with mock.patch(
        "termdeck.release_manager._atomic_write_bytes", side_effect=fail_once
    ), verification, pytest.raises(ReleaseError, match="rolled back"):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)

    assert layout.state_path.read_bytes() == state_before
    assert layout.launcher_path.read_bytes() == launcher_before
    assert load_managed_state(layout)["current_release_id"] == manifest["release_id"]


def test_legacy_rollback_rejects_backup_outside_managed_legacy_root(
    tmp_path: Path,
) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    legacy = bin_dir / "t1000"
    legacy_bytes = b"#!/bin/sh\necho legacy\n"
    legacy.write_bytes(legacy_bytes)
    legacy.chmod(0o755)
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state = load_managed_state(layout)
    outside = tmp_path / "outside-legacy"
    outside.write_bytes(legacy_bytes)
    outside.chmod(0o755)
    legacy_receipt = dict(state["legacy_launcher"])
    legacy_receipt["path"] = str(outside.absolute())
    state["legacy_launcher"] = legacy_receipt
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))
    with pytest.raises(ReleaseError, match="escaped"):
        rollback_release(root=root, bin_dir=bin_dir, to_legacy=True)


@pytest.mark.parametrize(
    "tamper_target",
    ["bootstrap", "launcher", "python", "state-serialization"],
)
def test_managed_status_detects_activation_identity_tamper(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    assert managed_release_status(root=root, bin_dir=bin_dir, verify=True)["ok"] is True

    if tamper_target == "bootstrap":
        layout.bootstrap_path.write_bytes(layout.bootstrap_path.read_bytes() + b"# tamper\n")
    elif tamper_target == "launcher":
        layout.launcher_path.write_bytes(layout.launcher_path.read_bytes() + b"# tamper\n")
    else:
        state = load_managed_state(layout)
        if tamper_target == "python":
            state["python_executable"] = str(Path("/usr/bin/true").resolve())
            layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))
        else:
            layout.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    status = managed_release_status(root=root, bin_dir=bin_dir, verify=True)
    assert status["ok"] is False
    assert status.get("error")


def test_installed_tree_and_status_detect_member_tamper(tmp_path: Path) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    release_dir = root / "releases" / str(manifest["release_id"])
    cli = release_dir / "site" / "termdeck" / "cli.py"
    cli.chmod(0o644)
    cli.write_text("tampered = True\n", encoding="utf-8")
    cli.chmod(0o444)
    with pytest.raises(ReleaseError, match="RECORD"):
        verify_installed_tree(release_dir)
    status = managed_release_status(root=root, bin_dir=bin_dir, verify=True)
    assert status["ok"] is False
    assert "RECORD" in str(status["error"])


def test_installed_tree_rejects_extra_files_symlinks_and_writable_payloads(
    tmp_path: Path,
) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    release_dir = root / "releases" / str(manifest["release_id"])
    site = release_dir / "site"
    assert all(
        path.stat().st_mode & 0o222 == 0
        for path in site.rglob("*")
        if path.is_file()
    )

    extra = site / "termdeck" / "extra.py"
    extra.write_text("unexpected = True\n", encoding="utf-8")
    extra.chmod(0o444)
    with pytest.raises(ReleaseError, match="file set"):
        verify_installed_tree(release_dir)
    extra.unlink()

    link = site / "termdeck" / "alias.py"
    link.symlink_to("cli.py")
    with pytest.raises(ReleaseError, match="symlink|unsafe"):
        verify_installed_tree(release_dir)
    link.unlink()

    cli = site / "termdeck" / "cli.py"
    cli.chmod(0o644)
    with pytest.raises(ReleaseError, match="writable"):
        verify_installed_tree(release_dir)
    cli.chmod(0o444)

    hardlink = site / "termdeck" / "hardlink.py"
    os.link(cli, hardlink)
    with pytest.raises(ReleaseError, match="hard link"):
        verify_installed_tree(release_dir)
    hardlink.unlink()
    assert verify_installed_tree(release_dir)["ok"] is True


def test_release_lock_rejects_symlinked_root_and_lock_file(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    root_link = tmp_path / "managed"
    root_link.symlink_to(real_root, target_is_directory=True)
    linked_layout = release_layout(root=root_link, bin_dir=tmp_path / "bin")
    with pytest.raises(ReleaseError, match="root.*real directory"):
        with release_lock(linked_layout):
            pass

    layout = release_layout(root=tmp_path / "safe-root", bin_dir=tmp_path / "bin")
    layout.root.mkdir(parents=True)
    target = tmp_path / "foreign-lock"
    target.write_text("foreign", encoding="utf-8")
    layout.lock_path.symlink_to(target)
    with pytest.raises(ReleaseError, match="open managed-release lock safely"):
        with release_lock(layout):
            pass


def test_release_lock_times_out_when_an_independent_process_holds_it(tmp_path: Path) -> None:
    layout = release_layout(root=tmp_path / "managed", bin_dir=tmp_path / "bin")
    layout.root.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time; "
                "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
                "fcntl.flock(fd,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(10)"
            ),
            str(layout.lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(ReleaseError, match="timed out"):
            with release_lock(layout, timeout=0.05):
                pass
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_semantic_runtime_snapshot_ignores_transient_client_identity(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite3"
    store = SessionStore(db)
    try:
        record = store.create(
            "research",
            "ai",
            "codex",
            tmp_path,
            window_id="agents",
            window_title="Agents",
        )
        record = store.update(
            record.id,
            status="running",
            recovery_command="codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154",
            recovery_cwd=str(tmp_path),
        )
    finally:
        store.close()

    class Backend:
        available = True
        transient_client_pid = 111

        def session_exists_many(self, records):
            return {item.id: True for item in records}

        def pane_pids(self, records):
            return {item.id: os.getpid() for item in records}

        def pane_target(self, item):
            return "%42"

    backend = Backend()
    first = semantic_runtime_snapshot(db_path=db, backend=backend)  # type: ignore[arg-type]
    backend.transient_client_pid = 999
    second = semantic_runtime_snapshot(db_path=db, backend=backend)  # type: ignore[arg-type]
    assert first == second
    assert first["row_count"] == 1
    assert first["rows"][0]["recovery_uuid"] == "019f71b3-fa32-7251-bac3-c628f34dd154"
    assert first["rows"][0]["pane_target"] == "%42"
