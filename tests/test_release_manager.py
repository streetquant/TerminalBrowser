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

import termdeck.release_manager as release_manager
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
    _bootstrap_source,
    _copy_snapshot,
    _launcher_source,
    _stage_release,
    _verify_staged_import,
    install_release,
    load_managed_state,
    managed_gui_process_status,
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
BOOT_ID = "11111111-2222-4333-8444-555555555555"


def build_info(seed: str, *, version: str = VERSION) -> dict[str, object]:
    info: dict[str, object] = {
        "schema": BUILD_INFO_SCHEMA,
        "version": version,
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
    dist_info: str | None = None,
    wheel_metadata: str | None = None,
    version: str = VERSION,
) -> dict[str, object]:
    info = build_info(seed, version=version)
    resolved_dist_info = dist_info or f"t1000-{version}.dist-info"
    members: dict[str, bytes] = {
        "termdeck/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "termdeck/cli.py": b"def main():\n    return 0\n",
        "termdeck/_build_info.json": canonical_json_bytes(info),
        f"{resolved_dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: t1000\nVersion: {version}\n\n"
        ).encode(),
        f"{resolved_dist_info}/WHEEL": (
            wheel_metadata
            or (
                "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            )
        ).encode(),
        f"{resolved_dist_info}/entry_points.txt": (
            "[console_scripts]\n" + entry_point
        ).encode(),
    }
    members.update(extra_members or {})
    record_name = f"{resolved_dist_info}/RECORD"
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


def artifact(
    tmp_path: Path,
    seed: str,
    *,
    version: str = VERSION,
) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / seed
    wheel = root / f"t1000-{version}-py3-none-any.whl"
    info = write_wheel(wheel, seed=seed, version=version)
    manifest_path = root / f"{wheel.stem}.release.json"
    manifest = write_manifest(manifest_path, wheel, info)
    return wheel, manifest_path, manifest


def thaw_directories(root: Path) -> None:
    """Restore owner-write permission so pytest can remove frozen test trees."""

    if not root.exists():
        return
    for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        if not directory.is_symlink():
            directory.chmod(0o755)
        for name in directories:
            child = directory / name
            if not child.is_symlink():
                child.chmod(0o755)


def managed_process_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, object, dict[str, object], Path]:
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    layout = release_layout(root=root, bin_dir=bin_dir)
    layout.root.mkdir(parents=True)
    layout.bin_dir.mkdir(parents=True)
    layout.bootstrap_path.write_text("# managed bootstrap\n", encoding="utf-8")
    layout.bootstrap_path.chmod(0o555)
    python = Path(sys.executable).resolve(strict=True)
    state = validate_managed_state(
        {
            "schema": MANAGED_STATE_SCHEMA,
            "generation": 1,
            "current_release_id": "a" * 64,
            "previous_release_id": None,
            "manifest_sha256": "b" * 64,
            "python_executable": str(python),
            "bootstrap_sha256": "c" * 64,
            "launcher_path": str(layout.launcher_path),
            "launcher_sha256": "d" * 64,
            "legacy_launcher": None,
        }
    )
    layout.state_path.write_bytes(canonical_json_bytes(state))
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text(BOOT_ID + "\n", encoding="ascii")
    return root, bin_dir, layout, state, proc_root


def write_fake_proc_process(
    proc_root: Path,
    pid: int,
    *,
    argv: tuple[bytes, ...],
    executable: Path,
    uid: int | None = None,
    start_ticks: int = 100,
    malformed_stat: bool = False,
    malformed_status: bool = False,
) -> Path:
    process_dir = proc_root / str(pid)
    process_dir.mkdir()
    if malformed_stat:
        (process_dir / "stat").write_text("malformed\n", encoding="utf-8")
    else:
        # Fields after ``comm`` begin at proc field 3. Start time is field 22,
        # therefore index 19 in this suffix.
        suffix = ["S", "1", *("0" for _ in range(17)), str(start_ticks), "0"]
        (process_dir / "stat").write_text(
            f"{pid} (t1000 process) {' '.join(suffix)}\n",
            encoding="utf-8",
        )
    process_uid = os.getuid() if uid is None else uid
    if malformed_status:
        status = "Name:\tt1000\nUid:\tbroken\n"
    else:
        status = (
            "Name:\tt1000\n"
            f"Uid:\t{process_uid}\t{process_uid}\t{process_uid}\t{process_uid}\n"
        )
    (process_dir / "status").write_text(status, encoding="utf-8")
    (process_dir / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (process_dir / "exe").symlink_to(executable)
    return process_dir


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


def test_install_upgrades_launcher_from_hash_recorded_in_managed_state(
    tmp_path: Path,
) -> None:
    wheel_a, manifest_a_path, _ = artifact(tmp_path, "a", version="0.2.12")
    wheel_b, manifest_b_path, _ = artifact(tmp_path, "b", version="0.2.13")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state = load_managed_state(layout)
    python = Path(str(state["python_executable"]))
    prior_launcher = (
        "#!/bin/sh\nset -eu\n"
        f'exec {python} -I {layout.bootstrap_path} "$@"\n'
    ).encode("utf-8")
    layout.launcher_path.write_bytes(prior_launcher)
    layout.launcher_path.chmod(0o755)
    state["launcher_sha256"] = sha256_bytes(prior_launcher)
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))

    with mock.patch("termdeck.release_manager._verify_staged_import"):
        upgraded = install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )

    assert upgraded["repaired"] is True
    assert upgraded["release_id"] != state["current_release_id"]
    assert layout.launcher_path.read_bytes() == _launcher_source(layout, python)
    assert load_managed_state(layout)["launcher_sha256"] == sha256_bytes(
        _launcher_source(layout, python)
    )
    thaw_directories(root / "releases" / str(upgraded["release_id"]))


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
    ("tail", "expected"),
    [
        ((), "default-gui"),
        ((b"gui",), "explicit-gui"),
        ((b"gui", b"--db", b"/db", b"--workspace=/workspace"), "explicit-gui"),
        ((b"--workspace", b"/workspace"), "workspace-gui"),
        ((b"--workspace=/workspace",), "workspace-gui"),
        ((b"release", b"info", b"--verify"), None),
        ((b"gui", b"--db"), None),
        ((b"gui", b"--workspace=/one", b"--workspace=/two"), None),
        ((b"--db", b"/db"), None),
    ],
)
def test_managed_gui_invocation_classification_is_exact(
    tail: tuple[bytes, ...],
    expected: str | None,
) -> None:
    assert release_manager._managed_gui_invocation_kind(tail) == expected


def test_internal_empty_argv_element_cannot_masquerade_as_default_gui(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        77,
        argv=prefix + (b"",),
        executable=python,
        start_ticks=700,
    )

    status = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )

    assert status["complete"] is True
    assert status["posture"] == "stopped"
    assert status["gui_count"] == 0
    assert status["ignored_process_count"] == 1
    raw = (proc_root / "77" / "cmdline").read_bytes()
    assert release_manager._parse_proc_argv(raw) == prefix + (b"",)


def test_managed_gui_process_status_accepts_no_bytecode_launcher_prefix(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (
        os.fsencode(str(python)),
        b"-I",
        b"-B",
        os.fsencode(str(bootstrap)),
    )
    write_fake_proc_process(
        proc_root,
        78,
        argv=prefix + (b"gui",),
        executable=python,
        start_ticks=701,
    )

    status = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )

    assert status["complete"] is True
    assert status["posture"] == "single-gui"
    assert status["gui_count"] == 1
    record = status["processes"][0]
    assert record["pid"] == 78
    assert record["launch_profile"] == "isolated-no-bytecode"
    assert record["argument_count"] == 5


def test_managed_gui_process_status_classifies_exact_forms_without_private_args(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))

    write_fake_proc_process(
        proc_root,
        301,
        argv=prefix,
        executable=python,
        start_ticks=300,
    )
    write_fake_proc_process(
        proc_root,
        101,
        argv=prefix
        + (b"gui", b"--db=/secret/db", b"--workspace", b"/secret/workspace"),
        executable=python,
        start_ticks=100,
    )
    write_fake_proc_process(
        proc_root,
        201,
        argv=prefix + (b"--workspace", b"/secret/default-workspace"),
        executable=python,
        start_ticks=200,
    )
    write_fake_proc_process(
        proc_root,
        401,
        argv=prefix + (b"release", b"info", b"--verify"),
        executable=python,
        start_ticks=400,
    )
    write_fake_proc_process(
        proc_root,
        501,
        argv=(prefix[0], b"-I", prefix[2] + b".lookalike"),
        executable=python,
        start_ticks=500,
    )
    wrong_executable = tmp_path / "wrong-python"
    wrong_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrong_executable.chmod(0o755)
    write_fake_proc_process(
        proc_root,
        601,
        argv=prefix,
        executable=wrong_executable,
        start_ticks=600,
    )
    write_fake_proc_process(
        proc_root,
        701,
        argv=prefix,
        executable=python,
        uid=uid + 1,
        start_ticks=700,
    )

    status = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )

    assert status["complete"] is True
    assert status["posture"] == "multiple-gui"
    assert status["gui_count"] == 3
    assert status["boot_id"] == BOOT_ID
    no_bytecode_prefix = (prefix[0], b"-I", b"-B", prefix[2])
    assert status["expected_identity"] == {
        "python_executable": str(python),
        "bootstrap_path": str(bootstrap),
        "argv_prefix_sha256": sha256_bytes(
            b"\0".join(no_bytecode_prefix) + b"\0"
        ),
        "accepted_argv_prefixes": [
            {
                "launch_profile": "isolated-no-bytecode",
                "argv_prefix_sha256": sha256_bytes(
                    b"\0".join(no_bytecode_prefix) + b"\0"
                ),
            },
            {
                "launch_profile": "legacy-isolated",
                "argv_prefix_sha256": sha256_bytes(b"\0".join(prefix) + b"\0"),
            },
        ],
    }
    assert status["activation"] == {
        "safe": False,
        "decision": "refuse-multiple",
        "requires_shutdown": False,
    }
    processes = status["processes"]
    assert [item["pid"] for item in processes] == [101, 201, 301]
    assert [item["invocation_kind"] for item in processes] == [
        "explicit-gui",
        "workspace-gui",
        "default-gui",
    ]
    assert all(item["uid"] == uid for item in processes)
    assert all(item["boot_id"] == BOOT_ID for item in processes)
    assert all(item["executable"] == str(python) for item in processes)
    assert status["scanned_pid_count"] == 7
    assert status["same_uid_pid_count"] == 7
    assert status["ignored_process_count"] == 4
    assert status["issues"] == []
    encoded = json.dumps(status, sort_keys=True)
    assert "/secret/" not in encoded
    expected_default = b"\0".join(prefix) + b"\0"
    default = next(item for item in processes if item["pid"] == 301)
    assert default["argument_count"] == 3
    assert default["cmdline_sha256"] == sha256_bytes(expected_default)


def test_managed_gui_process_status_reports_stopped_and_single_gui(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))

    stopped = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )
    assert stopped["posture"] == "stopped"
    assert stopped["activation"] == {
        "safe": True,
        "decision": "start",
        "requires_shutdown": False,
    }

    write_fake_proc_process(
        proc_root,
        42,
        argv=prefix + (b"gui", b"--workspace=/workspace"),
        executable=python,
        start_ticks=99,
    )
    running = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )
    assert running["posture"] == "single-gui"
    assert running["gui_count"] == 1
    assert running["activation"] == {
        "safe": True,
        "decision": "replace-single",
        "requires_shutdown": True,
    }


def test_managed_gui_process_status_fails_closed_on_incomplete_same_uid_scan(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        8,
        argv=prefix,
        executable=python,
        malformed_stat=True,
    )

    malformed = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=uid,
    )
    assert malformed["complete"] is False
    assert malformed["posture"] == "scan-incomplete"
    assert malformed["activation"]["safe"] is False
    assert malformed["issues"] == [
        {"code": "same-uid-entry-malformed", "count": 1}
    ]

    missing = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=tmp_path / "missing-proc",
        expected_uid=uid,
    )
    assert missing["posture"] == "scan-incomplete"
    assert missing["issues"] == [{"code": "proc-root-unavailable", "count": 1}]


@pytest.mark.parametrize(
    ("boot_value", "expected_code"),
    [
        (None, "boot-id-unavailable"),
        ("not-a-canonical-boot-id", "boot-id-invalid"),
    ],
)
def test_managed_gui_process_status_requires_canonical_boot_identity(
    tmp_path: Path,
    boot_value: str | None,
    expected_code: str,
) -> None:
    root, bin_dir, _layout, _state, proc_root = managed_process_fixture(tmp_path)
    boot_path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    if boot_value is None:
        boot_path.unlink()
    else:
        boot_path.write_text(boot_value + "\n", encoding="ascii")

    status = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=os.getuid(),
    )

    assert status["complete"] is False
    assert status["posture"] == "scan-incomplete"
    assert status["boot_id"] == ""
    assert status["issues"] == [{"code": expected_code, "count": 1}]


def test_managed_gui_process_status_rejects_numeric_proc_symlink_and_negative_uid(
    tmp_path: Path,
) -> None:
    root, bin_dir, _layout, _state, proc_root = managed_process_fixture(tmp_path)
    outside = tmp_path / "outside-process"
    outside.mkdir()
    (proc_root / "77").symlink_to(outside, target_is_directory=True)

    symlinked = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=os.getuid(),
    )
    assert symlinked["complete"] is False
    assert symlinked["posture"] == "scan-incomplete"
    assert symlinked["issues"] == [
        {"code": "same-uid-entry-invalid", "count": 1}
    ]

    invalid_uid = managed_gui_process_status(
        root=root,
        bin_dir=bin_dir,
        proc_root=proc_root,
        expected_uid=-1,
    )
    assert invalid_uid["complete"] is False
    assert invalid_uid["posture"] == "scan-incomplete"
    assert invalid_uid["issues"] == [
        {"code": "expected-uid-invalid", "count": 1}
    ]


def test_managed_gui_process_status_rechecks_candidate_after_executable_read(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    process_dir = write_fake_proc_process(
        proc_root,
        91,
        argv=prefix,
        executable=python,
        start_ticks=123,
    )
    first = release_manager._read_proc_observation(
        process_dir, include_executable=False
    )
    second = release_manager._read_proc_observation(
        process_dir, include_executable=True
    )
    reused = release_manager._ProcObservation(
        pid=second.pid,
        process_start_ticks=second.process_start_ticks + 1,
        real_uid=second.real_uid,
        effective_uid=second.effective_uid,
        argv=second.argv,
        cmdline_sha256=second.cmdline_sha256,
        executable=second.executable,
    )

    with mock.patch.object(
        release_manager,
        "_read_proc_observation",
        side_effect=[first, second, reused],
    ):
        status = managed_gui_process_status(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )

    assert status["complete"] is False
    assert status["posture"] == "scan-incomplete"
    assert status["raced_process_count"] == 1
    assert status["issues"] == [
        {"code": "same-uid-candidate-raced", "count": 1}
    ]


def test_managed_gui_process_status_handles_permission_pid_reuse_and_vanish(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    process_dir = write_fake_proc_process(
        proc_root,
        9,
        argv=prefix,
        executable=python,
        start_ticks=123,
    )
    first = release_manager._read_proc_observation(
        process_dir, include_executable=False
    )
    second = release_manager._read_proc_observation(
        process_dir, include_executable=True
    )

    with mock.patch.object(
        release_manager,
        "_read_proc_observation",
        side_effect=PermissionError("denied"),
    ):
        unreadable = managed_gui_process_status(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert unreadable["posture"] == "scan-incomplete"
    assert unreadable["issues"] == [
        {"code": "same-uid-entry-unreadable", "count": 1}
    ]

    reused = release_manager._ProcObservation(
        pid=second.pid,
        process_start_ticks=second.process_start_ticks + 1,
        real_uid=second.real_uid,
        effective_uid=second.effective_uid,
        argv=second.argv,
        cmdline_sha256=second.cmdline_sha256,
        executable=second.executable,
    )
    with mock.patch.object(
        release_manager,
        "_read_proc_observation",
        side_effect=[first, reused],
    ):
        raced = managed_gui_process_status(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert raced["complete"] is False
    assert raced["posture"] == "scan-incomplete"
    assert raced["raced_process_count"] == 1
    assert raced["issues"] == [
        {"code": "same-uid-candidate-raced", "count": 1}
    ]

    with mock.patch.object(
        release_manager,
        "_read_proc_observation",
        side_effect=[first, FileNotFoundError()],
    ):
        vanished = managed_gui_process_status(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert vanished["complete"] is False
    assert vanished["posture"] == "scan-incomplete"
    assert vanished["vanished_process_count"] == 1
    assert vanished["issues"] == [
        {"code": "same-uid-candidate-vanished", "count": 1}
    ]


def test_managed_release_status_keeps_process_posture_separate_from_integrity(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, _state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    for pid in (11, 12):
        write_fake_proc_process(
            proc_root,
            pid,
            argv=prefix,
            executable=python,
            start_ticks=pid,
        )

    with mock.patch(
        "termdeck.release_manager._verify_managed_activation",
        return_value={"current_release_id": "a" * 64, "legacy": False},
    ):
        status = managed_release_status(
            root=root,
            bin_dir=bin_dir,
            verify=False,
            proc_root=proc_root,
            expected_uid=uid,
        )

    assert status["ok"] is True
    assert status["runtime_processes"]["posture"] == "multiple-gui"
    assert status["runtime_processes"]["activation"]["safe"] is False
    assert status["verified"] is False


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


def test_release_layout_bin_dir_precedence_uses_runtime_environment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    inherited = tmp_path / "runtime-bin"
    explicit = tmp_path / "explicit-bin"

    with mock.patch.dict(
        os.environ,
        {"T1000_MANAGED_BIN_DIR": str(inherited)},
        clear=False,
    ):
        assert release_layout(root=root).bin_dir == inherited.absolute()
        assert release_layout(root=root, bin_dir=explicit).bin_dir == explicit.absolute()

    with mock.patch.dict(
        os.environ,
        {"T1000_MANAGED_BIN_DIR": "   "},
        clear=False,
    ):
        assert release_layout(root=root).bin_dir == (Path.home() / ".local" / "bin").absolute()


def test_custom_bin_environment_recovers_managed_status_without_explicit_argument(
    tmp_path: Path,
) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "e", version="0.2.13")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "custom-bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)

    try:
        with mock.patch.dict(
            os.environ,
            {"T1000_MANAGED_BIN_DIR": str(bin_dir)},
            clear=False,
        ):
            status = managed_release_status(root=root, verify=True)
        assert status["ok"] is True
        assert status["verified"] is True
        assert status["launcher"] == str((bin_dir / "t1000").absolute())
        assert status["state"]["current_release_id"] == manifest["release_id"]
        assert status["release"]["frozen_directories"] is True
    finally:
        thaw_directories(root)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.11", False),
        ("0.2.12", False),
        ("0.2.12.post1", False),
        ("0.2.13", True),
        ("0.2.13rc1", True),
        ("1.0.0", True),
        ("future", True),
    ],
)
def test_frozen_directory_policy_is_version_gated(
    version: str,
    expected: bool,
) -> None:
    info = build_info("a", version=version)
    assert release_manager._release_requires_frozen_directories(info) is expected


def test_launcher_bootstrap_and_staged_import_preserve_isolation_and_no_bytecode(
    tmp_path: Path,
) -> None:
    layout = release_layout(root=tmp_path / "managed", bin_dir=tmp_path / "bin")
    python = Path(sys.executable).resolve(strict=True)
    launcher = _launcher_source(layout, python).decode("utf-8")
    bootstrap = _bootstrap_source(layout, python).decode("utf-8")

    assert f"exec {python} -I -B {layout.bootstrap_path}" in launcher
    assert 'str(configured_python),\n                "-I",\n                "-B",' in bootstrap
    assert 'str(bootstrap),\n                *sys.argv[1:],' in bootstrap
    assert "sys.dont_write_bytecode = True" in bootstrap
    assert 'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"' in bootstrap
    managed_bin_assignment = 'os.environ["T1000_MANAGED_BIN_DIR"] = str(launcher.parent)'
    assert managed_bin_assignment in bootstrap
    assert bootstrap.index(managed_bin_assignment) > bootstrap.index("latest_state =")
    assert bootstrap.index(managed_bin_assignment) < bootstrap.index("sys.path.insert(0, str(site))")

    with mock.patch("termdeck.release_manager._run") as run:
        _verify_staged_import(tmp_path / "release", python)
    argv = run.call_args.args[0]
    assert argv[:4] == [str(python), "-I", "-B", "-c"]


def test_new_policy_install_freezes_directories_and_blocks_bytecode_creation(
    tmp_path: Path,
) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "f", version="0.2.13")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        result = install_release(
            wheel,
            manifest_path,
            root=root,
            bin_dir=bin_dir,
            python=Path(sys.executable),
        )
    release_dir = root / "releases" / str(manifest["release_id"])
    site = release_dir / "site"
    try:
        directories = [release_dir, *sorted(path for path in release_dir.rglob("*") if path.is_dir())]
        assert directories
        assert all(stat.S_IMODE(path.lstat().st_mode) == 0o555 for path in directories)
        assert result["release_id"] == manifest["release_id"]
        verified = verify_installed_tree(release_dir)
        assert verified["frozen_directories"] is True

        with pytest.raises(PermissionError):
            (site / "termdeck" / "same-user-write.py").write_text(
                "unexpected = True\n", encoding="utf-8"
            )

        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        code = (
            "import sys; from pathlib import Path; "
            "site=Path(sys.argv[1]).resolve(); sys.path.insert(0,str(site)); "
            "import termdeck,termdeck.cli; "
            "assert Path(termdeck.__file__).resolve().is_relative_to(site)"
        )
        subprocess.run(
            [str(Path(sys.executable).resolve()), "-I", "-c", code, str(site)],
            cwd=tmp_path,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert not list(release_dir.rglob("*.pyc"))
        assert not list(release_dir.rglob("__pycache__"))
    finally:
        thaw_directories(release_dir)


def test_legacy_release_directories_remain_backward_verifiable(tmp_path: Path) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "b", version="0.2.12")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    release_dir = root / "releases" / str(manifest["release_id"])
    try:
        assert stat.S_IMODE(release_dir.lstat().st_mode) & 0o200
        verified = verify_installed_tree(release_dir)
        assert verified["frozen_directories"] is False
    finally:
        thaw_directories(release_dir)


def test_new_policy_verifier_rejects_writable_directory(tmp_path: Path) -> None:
    wheel, manifest_path, manifest = artifact(tmp_path, "c", version="0.2.13")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    release_dir = root / "releases" / str(manifest["release_id"])
    target = release_dir / "site" / "termdeck"
    try:
        target.chmod(0o755)
        with pytest.raises(ReleaseError, match="directory.*writable|frozen directory"):
            verify_installed_tree(release_dir)
    finally:
        thaw_directories(release_dir)


def test_failed_verification_cleans_a_frozen_unique_staging_tree(tmp_path: Path) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "c", version="0.2.13")
    verified = verify_wheel(wheel, manifest_path=manifest_path)
    layout = release_layout(root=tmp_path / "managed", bin_dir=tmp_path / "bin")

    def freeze_then_fail(path: str | Path) -> dict[str, object]:
        stage = Path(path)
        for current, _directories, _files in os.walk(stage, topdown=False, followlinks=False):
            Path(current).chmod(0o555)
        raise ReleaseError("simulated verification failure after freeze")

    with mock.patch(
        "termdeck.release_manager.verify_installed_tree",
        side_effect=freeze_then_fail,
    ), pytest.raises(ReleaseError, match="after freeze"):
        _stage_release(layout, verified, manifest_path)

    leftovers = list(layout.releases_dir.glob(".staging-*"))
    try:
        assert leftovers == []
    finally:
        for leftover in leftovers:
            thaw_directories(leftover)
            shutil.rmtree(leftover, ignore_errors=True)


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
