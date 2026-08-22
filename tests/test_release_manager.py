from __future__ import annotations

import base64
import copy
import concurrent.futures
from contextlib import nullcontext
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
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

# Synchronization-only test horizons must remain bounded but cannot assume an
# otherwise healthy worker is scheduled within one or two seconds on a host
# experiencing sustained kernel I/O pressure. Tests that intentionally verify
# production timeout behavior patch the production timeout to milliseconds and
# are not governed by these values.
_TEST_CONCURRENCY_START_TIMEOUT_SECONDS = 120.0
_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS = 120.0
_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS = 180.0
_TEST_CONCURRENCY_BARRIER_TIMEOUT_SECONDS = 120.0
_TEST_CONCURRENCY_SETTLE_SECONDS = 2.0


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


def _zip_info(
    name: str,
    *,
    mode: int = stat.S_IFREG | 0o644,
    create_system: int = 3,
    internal_attr: int = 0,
    external_low_attr: int = 0,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
    info.create_system = create_system
    info.internal_attr = internal_attr
    info.external_attr = (mode << 16) | external_low_attr
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def write_wheel(
    path: Path,
    *,
    seed: str = "a",
    extra_members: dict[str, bytes] | None = None,
    special_modes: dict[str, int] | None = None,
    default_mode: int = stat.S_IFREG | 0o644,
    create_systems: dict[str, int] | None = None,
    internal_attrs: dict[str, int] | None = None,
    external_low_attrs: dict[str, int] | None = None,
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
            mode = (special_modes or {}).get(name, default_mode)
            archive.writestr(
                _zip_info(
                    name,
                    mode=mode,
                    create_system=(create_systems or {}).get(name, 3),
                    internal_attr=(internal_attrs or {}).get(name, 0),
                    external_low_attr=(external_low_attrs or {}).get(name, 0),
                ),
                content,
            )
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


def wheel_payload_identity(path: Path) -> tuple[tuple[object, ...], ...]:
    with zipfile.ZipFile(path) as archive:
        return tuple(
            (
                info.filename,
                hashlib.sha256(archive.read(info)).hexdigest(),
                info.CRC,
                info.file_size,
                info.compress_size,
                info.compress_type,
                info.date_time,
                info.header_offset,
                info.flag_bits,
                info.extra,
                info.comment,
            )
            for info in archive.infolist()
        )


def process_umask() -> int:
    status = Path("/proc/self/status")
    if not status.is_file():
        pytest.skip("process umask observation requires Linux /proc")
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith("Umask:"):
            return int(line.split(":", 1)[1].strip(), 8)
    raise AssertionError("/proc/self/status has no Umask field")


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


def process_observer_receipt(
    *,
    layout,
    state: dict[str, object],
    status: dict[str, object],
    expected_uid: int,
    source_module: Path | None = None,
) -> dict[str, object]:
    module = (
        Path(release_manager.__file__).resolve(strict=True)
        if source_module is None
        else source_module.resolve(strict=False)
    )
    return {
        "schema": release_manager._PROCESS_OBSERVER_RECEIPT_SCHEMA,
        "ok": True,
        "error_code": "",
        "observer": "systemd-user-manager",
        "source_root": str(module.parent.parent),
        "source_module": str(module),
        "root": str(layout.root),
        "bin_dir": str(layout.bin_dir),
        "current_release_id": state["current_release_id"],
        "managed_state_sha256": sha256_bytes(canonical_json_bytes(state)),
        "expected_uid": expected_uid,
        "stable_samples": 2,
        "stable": True,
        "state_stable": True,
        "status": status,
    }


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


def init_buildable_git_project(root: Path) -> None:
    """Create the smallest real t1000 wheel source accepted by release verification."""

    package = root / "termdeck"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "t1000"\n'
        'version = "0.2.14"\n'
        'description = "Minimal release reproducibility fixture"\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.10"\n'
        "dependencies = []\n\n"
        "[project.scripts]\n"
        't1000 = "termdeck.cli:main"\n\n'
        "[tool.setuptools.packages.find]\n"
        'where = ["."]\n'
        'include = ["termdeck*"]\n\n'
        "[tool.setuptools.package-data]\n"
        'termdeck = ["_build_info.json"]\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Minimal T1000 fixture\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        '__version__ = "0.2.14"\n', encoding="utf-8"
    )
    (package / "cli.py").write_text(
        "def main() -> int:\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (package / "_build_info.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"], cwd=root, check=True
    )
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


def test_canonicalize_built_wheel_metadata_is_mode_only_and_idempotent(
    tmp_path: Path,
) -> None:
    version = "0.2.14"
    masked = tmp_path / "masked" / f"t1000-{version}-py3-none-any.whl"
    canonical = tmp_path / "canonical" / f"t1000-{version}-py3-none-any.whl"
    write_wheel(masked, version=version, default_mode=stat.S_IFREG | 0o600)
    write_wheel(canonical, version=version)
    masked_payload = wheel_payload_identity(masked)
    canonical_payload = wheel_payload_identity(canonical)
    assert masked_payload == canonical_payload

    release_manager._canonicalize_built_wheel_metadata(masked)
    release_manager._canonicalize_built_wheel_metadata(canonical)

    assert masked.read_bytes() == canonical.read_bytes()
    assert wheel_payload_identity(masked) == masked_payload
    assert wheel_payload_identity(canonical) == canonical_payload
    with zipfile.ZipFile(masked) as archive:
        for info in archive.infolist():
            assert info.create_system == 3
            assert info.internal_attr == 0
            assert info.external_attr & 0xFFFF == 0
            assert release_manager._wheel_member_mode(info) == stat.S_IFREG | 0o644

    once = masked.read_bytes()
    release_manager._canonicalize_built_wheel_metadata(masked)
    assert masked.read_bytes() == once


def test_canonicalize_built_wheel_metadata_rejects_trailing_bytes_without_mutation(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / WHEEL_NAME
    write_wheel(wheel)
    wheel.write_bytes(wheel.read_bytes() + b"trailing")
    before = wheel.read_bytes()
    with pytest.raises(ReleaseError, match="trailing|end of central directory"):
        release_manager._canonicalize_built_wheel_metadata(wheel)
    assert wheel.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "zip64-entries",
        "multi-disk",
        "central-size",
        "central-offset",
        "archive-comment",
        "central-signature",
        "member-zip64-size",
        "member-zip64-offset",
        "member-zip64-version",
    ],
)
def test_canonicalize_built_wheel_metadata_fails_closed_on_malformed_zip_structure(
    tmp_path: Path,
    mutation: str,
) -> None:
    wheel = tmp_path / f"{mutation}.whl"
    write_wheel(wheel)
    content = bytearray(wheel.read_bytes())
    eocd = content.rfind(b"PK\x05\x06")
    assert eocd >= 0
    if mutation == "zip64-entries":
        struct.pack_into("<H", content, eocd + 10, 0xFFFF)
    elif mutation == "multi-disk":
        struct.pack_into("<H", content, eocd + 4, 1)
    elif mutation == "central-size":
        size = struct.unpack_from("<I", content, eocd + 12)[0]
        struct.pack_into("<I", content, eocd + 12, size + 1)
    elif mutation == "central-offset":
        offset = struct.unpack_from("<I", content, eocd + 16)[0]
        struct.pack_into("<I", content, eocd + 16, offset + 1)
    elif mutation == "archive-comment":
        struct.pack_into("<H", content, eocd + 20, 1)
        content.extend(b"x")
    elif mutation == "central-signature":
        offset = struct.unpack_from("<I", content, eocd + 16)[0]
        content[offset : offset + 4] = b"BAD!"
    elif mutation == "member-zip64-size":
        offset = struct.unpack_from("<I", content, eocd + 16)[0]
        struct.pack_into("<I", content, offset + 20, 0xFFFFFFFF)
    elif mutation == "member-zip64-offset":
        offset = struct.unpack_from("<I", content, eocd + 16)[0]
        struct.pack_into("<I", content, offset + 42, 0xFFFFFFFF)
    elif mutation == "member-zip64-version":
        offset = struct.unpack_from("<I", content, eocd + 16)[0]
        struct.pack_into("<H", content, offset + 6, 45)
    else:
        raise AssertionError(mutation)
    wheel.write_bytes(content)
    before = wheel.read_bytes()
    with pytest.raises(ReleaseError):
        release_manager._canonicalize_built_wheel_metadata(wheel)
    assert wheel.read_bytes() == before


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.13", False),
        ("0.2.13.post1", False),
        ("0.2.14", True),
        ("0.2.14rc1", True),
        ("1.0.0", True),
        ("future", True),
        (None, True),
    ],
)
def test_canonical_wheel_metadata_policy_is_version_gated(
    version: object,
    expected: bool,
) -> None:
    assert release_manager._release_requires_canonical_wheel_metadata(version) is expected


def test_verify_canonical_wheel_metadata_is_version_gated(tmp_path: Path) -> None:
    current = "0.2.14"
    current_record = f"t1000-{current}.dist-info/RECORD"
    canonical = tmp_path / "canonical" / f"t1000-{current}-py3-none-any.whl"
    info = write_wheel(canonical, version=current)
    manifest = canonical.with_suffix(".release.json")
    write_manifest(manifest, canonical, info)
    verify_wheel(canonical, manifest_path=manifest)

    noncanonical = tmp_path / "noncanonical" / canonical.name
    info = write_wheel(
        noncanonical,
        version=current,
        special_modes={current_record: stat.S_IFREG | 0o664},
    )
    noncanonical_manifest = noncanonical.with_suffix(".release.json")
    write_manifest(noncanonical_manifest, noncanonical, info)
    with pytest.raises(ReleaseError, match="canonical wheel metadata"):
        verify_wheel(noncanonical, manifest_path=noncanonical_manifest)

    non_unix = tmp_path / "non-unix" / canonical.name
    info = write_wheel(
        non_unix,
        version=current,
        create_systems={"termdeck/__init__.py": 0},
    )
    non_unix_manifest = non_unix.with_suffix(".release.json")
    write_manifest(non_unix_manifest, non_unix, info)
    with pytest.raises(ReleaseError, match="canonical wheel metadata"):
        verify_wheel(non_unix, manifest_path=non_unix_manifest)

    legacy = "0.2.13"
    legacy_record = f"t1000-{legacy}.dist-info/RECORD"
    legacy_wheel = tmp_path / "legacy" / f"t1000-{legacy}-py3-none-any.whl"
    info = write_wheel(
        legacy_wheel,
        version=legacy,
        special_modes={legacy_record: stat.S_IFREG | 0o664},
    )
    legacy_manifest = legacy_wheel.with_suffix(".release.json")
    write_manifest(legacy_manifest, legacy_wheel, info)
    verify_wheel(legacy_wheel, manifest_path=legacy_manifest)

    legacy_noncanonical = (
        tmp_path / "legacy-noncanonical" / f"t1000-{legacy}-py3-none-any.whl"
    )
    info = write_wheel(
        legacy_noncanonical,
        version=legacy,
        default_mode=stat.S_IFREG | 0o600,
    )
    legacy_noncanonical_manifest = legacy_noncanonical.with_suffix(".release.json")
    write_manifest(legacy_noncanonical_manifest, legacy_noncanonical, info)
    with pytest.raises(ReleaseError, match="legacy wheel metadata"):
        verify_wheel(
            legacy_noncanonical,
            manifest_path=legacy_noncanonical_manifest,
        )


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("mode-0600", {"special_modes": {"termdeck/__init__.py": stat.S_IFREG | 0o600}}),
        ("mode-0755", {"special_modes": {"termdeck/__init__.py": stat.S_IFREG | 0o755}}),
        ("internal", {"internal_attrs": {"termdeck/__init__.py": 1}}),
        ("low-external", {"external_low_attrs": {"termdeck/__init__.py": 1}}),
        (
            "directory",
            {
                "extra_members": {"termdeck/data/": b""},
                "special_modes": {"termdeck/data/": stat.S_IFDIR | 0o700},
                "external_low_attrs": {"termdeck/data/": 0x10},
            },
        ),
    ],
)
def test_verify_rejects_each_noncanonical_current_wheel_metadata_dimension(
    tmp_path: Path,
    case: str,
    kwargs: dict[str, object],
) -> None:
    version = "0.2.14"
    wheel = tmp_path / case / f"t1000-{version}-py3-none-any.whl"
    info = write_wheel(wheel, version=version, **kwargs)
    manifest = wheel.with_suffix(".release.json")
    write_manifest(manifest, wheel, info)
    with pytest.raises(ReleaseError, match="canonical wheel metadata"):
        verify_wheel(wheel, manifest_path=manifest)


def test_build_release_is_reproducible_across_caller_umask(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    init_buildable_git_project(source)
    runner = tmp_path / "build_under_umask.py"
    runner.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "requested = int(sys.argv[3], 8)\n"
        "os.umask(requested)\n"
        "def observed_umask():\n"
        "    for line in Path('/proc/self/status').read_text().splitlines():\n"
        "        if line.startswith('Umask:'):\n"
        "            return int(line.split(':', 1)[1].strip(), 8)\n"
        "    raise RuntimeError('missing process umask')\n"
        "from termdeck.release_manager import build_release\n"
        "result = build_release(sys.argv[1], sys.argv[2], "
        "python=sys.executable)\n"
        "result['caller_umask_after'] = observed_umask()\n"
        "result['caller_umask_expected'] = requested\n"
        "print(json.dumps(result, sort_keys=True))\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONPATH": str(repository), "PYTHONDONTWRITEBYTECODE": "1"}
    before_umask = process_umask()
    results = []
    for value in ("022", "077"):
        output = tmp_path / f"release-{value}"
        completed = subprocess.run(
            [sys.executable, "-B", str(runner), str(source), str(output), value],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=300,
        )
        assert completed.stderr == ""
        results.append(json.loads(completed.stdout))
    assert process_umask() == before_umask

    first, second = results
    first_wheel = Path(str(first["wheel"]))
    second_wheel = Path(str(second["wheel"]))
    first_manifest = Path(str(first["manifest"]))
    second_manifest = Path(str(second["manifest"]))
    assert first["caller_umask_after"] == first["caller_umask_expected"] == 0o22
    assert second["caller_umask_after"] == second["caller_umask_expected"] == 0o77
    assert first["release_id"] == second["release_id"]
    assert first["wheel_sha256"] == second["wheel_sha256"]
    assert first_wheel.read_bytes() == second_wheel.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    with zipfile.ZipFile(first_wheel) as archive:
        assert archive.infolist()
        assert all(
            info.create_system == 3
            and info.internal_attr == 0
            and info.external_attr & 0xFFFF == 0
            and release_manager._wheel_member_mode(info) == stat.S_IFREG | 0o644
            for info in archive.infolist()
        )


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

    python = Path(str(state["python_executable"]))
    target = target_activation_bundle(layout, python)
    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=target,
    ):
        rolled = rollback_release(root=root, bin_dir=bin_dir)
    assert rolled["release_id"] == manifest_a["release_id"]
    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=target,
    ), mock.patch("termdeck.release_manager._verify_staged_import"):
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


def test_install_upgrades_verified_selected_release_activation(
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
    prior = target_activation_bundle(layout, python, label="selected-release")
    layout.bootstrap_path.write_bytes(prior.bootstrap)
    layout.bootstrap_path.chmod(0o755)
    layout.launcher_path.write_bytes(prior.launcher)
    layout.launcher_path.chmod(0o755)
    state["bootstrap_sha256"] = prior.bootstrap_sha256
    state["launcher_sha256"] = prior.launcher_sha256
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=prior,
    ), mock.patch("termdeck.release_manager._verify_staged_import"):
        upgraded = install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )

    assert upgraded["repaired"] is True
    assert upgraded["release_id"] != state["current_release_id"]
    assert layout.bootstrap_path.read_bytes() == _bootstrap_source(layout, python)
    assert layout.launcher_path.read_bytes() == _launcher_source(layout, python)
    repaired = load_managed_state(layout)
    assert repaired["bootstrap_sha256"] == sha256_bytes(_bootstrap_source(layout, python))
    assert repaired["launcher_sha256"] == sha256_bytes(_launcher_source(layout, python))
    thaw_directories(root / "releases" / str(upgraded["release_id"]))


def target_activation_bundle(
    layout: object,
    python: Path,
    *,
    label: str = "target",
) -> object:
    bootstrap = (
        f"#!/usr/bin/{python.name}\n# {label} bootstrap for {layout.root}\n"
    ).encode("utf-8")
    launcher = (
        "#!/bin/sh\nset -eu\n"
        f'exec {python} -I {layout.bootstrap_path} "$@"\n'
        f"# {label}\n"
    ).encode("utf-8")
    return release_manager._ActivationBundle(
        bootstrap=bootstrap,
        launcher=launcher,
        bootstrap_sha256=sha256_bytes(bootstrap),
        launcher_sha256=sha256_bytes(launcher),
        origin="selected-release",
    )


def test_install_rejects_arbitrary_state_recorded_historical_activation(
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
    canonical = target_activation_bundle(layout, python, label="canonical-selected")
    arbitrary = target_activation_bundle(layout, python, label="arbitrary-recorded")
    layout.bootstrap_path.write_bytes(arbitrary.bootstrap)
    layout.bootstrap_path.chmod(0o755)
    layout.launcher_path.write_bytes(arbitrary.launcher)
    layout.launcher_path.chmod(0o755)
    state["bootstrap_sha256"] = arbitrary.bootstrap_sha256
    state["launcher_sha256"] = arbitrary.launcher_sha256
    state_bytes = canonical_json_bytes(validate_managed_state(state))
    layout.state_path.write_bytes(state_bytes)

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=canonical,
    ), mock.patch("termdeck.release_manager._verify_staged_import"), pytest.raises(
        ReleaseError,
        match="non-canonical|selected release",
    ):
        install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )

    assert layout.state_path.read_bytes() == state_bytes
    assert layout.bootstrap_path.read_bytes() == arbitrary.bootstrap
    assert layout.launcher_path.read_bytes() == arbitrary.launcher


@pytest.mark.parametrize(
    "damage",
    ["bootstrap-missing", "bootstrap-tampered", "launcher-missing"],
)
def test_install_repairs_damaged_files_from_authenticated_historical_state(
    tmp_path: Path,
    damage: str,
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
    prior = target_activation_bundle(layout, python, label="authenticated-historical")
    layout.bootstrap_path.write_bytes(prior.bootstrap)
    layout.bootstrap_path.chmod(0o755)
    layout.launcher_path.write_bytes(prior.launcher)
    layout.launcher_path.chmod(0o755)
    state["bootstrap_sha256"] = prior.bootstrap_sha256
    state["launcher_sha256"] = prior.launcher_sha256
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))
    if damage == "bootstrap-missing":
        layout.bootstrap_path.unlink()
    elif damage == "bootstrap-tampered":
        layout.bootstrap_path.write_bytes(b"#!/bin/sh\n# damaged bootstrap\n")
        layout.bootstrap_path.chmod(0o755)
    else:
        layout.launcher_path.unlink()

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=prior,
    ), mock.patch("termdeck.release_manager._verify_staged_import"):
        repaired = install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )

    assert repaired["repaired"] is True
    assert layout.bootstrap_path.read_bytes() == _bootstrap_source(layout, python)
    assert layout.launcher_path.read_bytes() == _launcher_source(layout, python)
    current = load_managed_state(layout)
    assert current["bootstrap_sha256"] == sha256_bytes(_bootstrap_source(layout, python))
    assert current["launcher_sha256"] == sha256_bytes(_launcher_source(layout, python))


def test_install_authenticates_historical_activation_from_wheel_not_extracted_site(
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
    prior = target_activation_bundle(layout, python, label="wheel-authority")
    layout.bootstrap_path.write_bytes(prior.bootstrap)
    layout.bootstrap_path.chmod(0o755)
    layout.launcher_path.write_bytes(prior.launcher)
    layout.launcher_path.chmod(0o755)
    state["bootstrap_sha256"] = prior.bootstrap_sha256
    state["launcher_sha256"] = prior.launcher_sha256
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))
    release_dir = root / "releases" / str(state["current_release_id"])
    contamination = release_dir / "site" / "termdeck" / "unexpected.py"
    contamination.write_text("unexpected = True\n", encoding="utf-8")

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=prior,
    ) as derive, mock.patch("termdeck.release_manager._verify_staged_import"):
        installed = install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )

    derive.assert_called_once_with(layout, str(state["current_release_id"]), python)
    assert installed["ok"] is True
    assert contamination.exists()


@pytest.mark.parametrize("linked_component", ["release", "artifacts"])
def test_selected_release_activation_rejects_symlinked_artifact_container(
    tmp_path: Path,
    linked_component: str,
) -> None:
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    layout = release_layout(root=root, bin_dir=bin_dir)
    external = tmp_path / "external-release"
    artifacts = external / "artifacts"
    wheel = artifacts / f"t1000-{VERSION}-py3-none-any.whl"
    manager_source = (
        "from pathlib import Path\n"
        "class Layout:\n"
        "    def __init__(self, root, bin_dir):\n"
        "        self.root = Path(root).absolute()\n"
        "        self.bin_dir = Path(bin_dir).absolute()\n"
        "        self.bootstrap_path = self.root / 'bootstrap.py'\n"
        "def release_layout(*, root=None, bin_dir=None):\n"
        "    return Layout(root, bin_dir)\n"
        "def _bootstrap_source(layout, python):\n"
        "    return f'BOOT:{layout.root}:{layout.bin_dir}:{python}'.encode()\n"
        "def _launcher_source(layout, python):\n"
        "    return f'LAUNCH:{layout.bootstrap_path}:{python}'.encode()\n"
    ).encode("utf-8")
    info = write_wheel(
        wheel,
        seed="d",
        extra_members={"termdeck/release_manager.py": manager_source},
    )
    manifest_path = external / "manifest.json"
    manifest = write_manifest(manifest_path, wheel, info)
    wheel.chmod(0o444)
    manifest_path.chmod(0o444)
    release_id = str(manifest["release_id"])
    layout.releases_dir.mkdir(parents=True)
    selected = layout.releases_dir / release_id
    if linked_component == "release":
        selected.symlink_to(external, target_is_directory=True)
    else:
        selected.mkdir()
        shutil.copyfile(manifest_path, selected / "manifest.json")
        (selected / "manifest.json").chmod(0o444)
        (selected / "artifacts").symlink_to(artifacts, target_is_directory=True)

    with pytest.raises(ReleaseError, match="real directory|symlink|unsafe"):
        release_manager._derive_release_activation_bundle(
            layout,
            release_id,
            Path(sys.executable).resolve(strict=True),
        )


@pytest.mark.parametrize(
    ("stream", "program", "message"),
    [
        ("stdout", "import os; os.write(1, b'x' * 4096)", "stdout"),
        ("stderr", "import os; os.write(2, b'x' * 4096)", "stderr"),
    ],
)
def test_bounded_process_runner_terminates_output_overflow(
    tmp_path: Path,
    stream: str,
    program: str,
    message: str,
) -> None:
    with pytest.raises(ReleaseError, match=message):
        release_manager._run_bounded_output(
            [str(Path(sys.executable).resolve()), "-I", "-B", "-c", program],
            cwd=tmp_path,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout=5.0,
            stdout_limit=128,
            stderr_limit=128,
        )


def test_bounded_process_runner_kills_descendant_after_group_leader_exits(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "descendant.pid"
    descendant = (
        "import os,sys,time; "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    leader = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-I', '-B', '-c', sys.argv[1], sys.argv[2]]); "
        "raise SystemExit(0)"
    )
    descendant_pid: int | None = None
    try:
        with pytest.raises(ReleaseError, match="timed out"):
            release_manager._run_bounded_output(
                [
                    str(Path(sys.executable).resolve()),
                    "-I",
                    "-B",
                    "-c",
                    leader,
                    descendant,
                    str(pid_path),
                ],
                cwd=tmp_path,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                timeout=0.2,
                stdout_limit=128,
                stderr_limit=128,
            )
        for _ in range(100):
            if pid_path.exists() and pid_path.read_text(encoding="ascii").strip():
                descendant_pid = int(pid_path.read_text(encoding="ascii").strip())
                break
            time.sleep(0.01)
        assert descendant_pid is not None
        for _ in range(100):
            if not (Path("/proc") / str(descendant_pid)).exists():
                break
            time.sleep(0.01)
        assert not (Path("/proc") / str(descendant_pid)).exists()
    finally:
        if descendant_pid is not None and (Path("/proc") / str(descendant_pid)).exists():
            try:
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass


def test_bounded_process_runner_returns_exact_bytes_and_times_out(tmp_path: Path) -> None:
    completed = release_manager._run_bounded_output(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            "-c",
            "import os; os.write(1,b'out'); os.write(2,b'err')",
        ],
        cwd=tmp_path,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=5.0,
        stdout_limit=128,
        stderr_limit=128,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"out"
    assert completed.stderr == b"err"
    with pytest.raises(ReleaseError, match="timed out"):
        release_manager._run_bounded_output(
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-B",
                "-c",
                "import time; time.sleep(5)",
            ],
            cwd=tmp_path,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout=0.05,
            stdout_limit=128,
            stderr_limit=128,
        )


def test_selected_release_activation_derivation_is_isolated_and_path_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    layout = release_layout(root=root, bin_dir=bin_dir)
    target_source = (
        "from pathlib import Path\n"
        "class Layout:\n"
        "    def __init__(self, root, bin_dir):\n"
        "        self.root = Path(root).absolute()\n"
        "        self.bin_dir = Path(bin_dir).absolute()\n"
        "        self.bootstrap_path = self.root / 'bootstrap.py'\n"
        "def release_layout(*, root=None, bin_dir=None):\n"
        "    return Layout(root, bin_dir)\n"
        "def _bootstrap_source(layout, python):\n"
        "    return f'BOOT:{layout.root}:{layout.bin_dir}:{python}'.encode()\n"
        "def _launcher_source(layout, python):\n"
        "    return f'LAUNCH:{layout.bootstrap_path}:{python}'.encode()\n"
    ).encode("utf-8")
    wheel = tmp_path / "target" / WHEEL_NAME
    info = write_wheel(
        wheel,
        extra_members={"termdeck/release_manager.py": target_source},
    )
    manifest_source = wheel.with_suffix(".release.json")
    manifest = write_manifest(manifest_source, wheel, info)
    release_id = str(manifest["release_id"])
    release_dir = root / "releases" / release_id
    artifacts = release_dir / "artifacts"
    artifacts.mkdir(parents=True)
    installed_wheel = artifacts / wheel.name
    shutil.copyfile(wheel, installed_wheel)
    installed_wheel.chmod(0o444)
    installed_manifest = release_dir / "manifest.json"
    shutil.copyfile(manifest_source, installed_manifest)
    installed_manifest.chmod(0o444)
    python = Path(sys.executable).resolve(strict=True)

    bundle = release_manager._derive_release_activation_bundle(
        layout,
        release_id,
        python,
    )

    assert bundle.origin == "selected-release"
    assert bundle.bootstrap == f"BOOT:{layout.root}:{layout.bin_dir}:{python}".encode()
    assert bundle.launcher == f"LAUNCH:{layout.bootstrap_path}:{python}".encode()
    assert bundle.bootstrap_sha256 == sha256_bytes(bundle.bootstrap)
    assert bundle.launcher_sha256 == sha256_bytes(bundle.launcher)
    assert list(release_dir.rglob("*.pyc")) == []
    assert list(release_dir.rglob("__pycache__")) == []


def test_managed_rollback_publishes_target_activation_and_reinstall_repairs_it(
    tmp_path: Path,
) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    python = Path(str(load_managed_state(layout)["python_executable"]))
    target = target_activation_bundle(layout, python)

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=target,
    ):
        rolled = rollback_release(
            root=root,
            bin_dir=bin_dir,
            release_id=str(manifest_a["release_id"]),
        )

    state = load_managed_state(layout)
    assert rolled["release_id"] == manifest_a["release_id"]
    assert rolled["activation_origin"] == "selected-release"
    assert state["current_release_id"] == manifest_a["release_id"]
    assert state["previous_release_id"] == manifest_b["release_id"]
    assert state["bootstrap_sha256"] == target.bootstrap_sha256
    assert state["launcher_sha256"] == target.launcher_sha256
    assert layout.bootstrap_path.read_bytes() == target.bootstrap
    assert layout.launcher_path.read_bytes() == target.launcher

    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=target,
    ), mock.patch("termdeck.release_manager._verify_staged_import"):
        reinstalled = install_release(
            wheel_b,
            manifest_b_path,
            root=root,
            bin_dir=bin_dir,
        )
    repaired_state = load_managed_state(layout)
    assert reinstalled["release_id"] == manifest_b["release_id"]
    assert reinstalled["repaired"] is True
    assert layout.bootstrap_path.read_bytes() == _bootstrap_source(layout, python)
    assert layout.launcher_path.read_bytes() == _launcher_source(layout, python)
    assert repaired_state["bootstrap_sha256"] == sha256_bytes(
        _bootstrap_source(layout, python)
    )
    assert repaired_state["launcher_sha256"] == sha256_bytes(
        _launcher_source(layout, python)
    )


@pytest.mark.parametrize("failure_target", ["bootstrap", "state", "launcher", "verify"])
def test_managed_rollback_failure_restores_complete_activation_preimage(
    tmp_path: Path,
    failure_target: str,
) -> None:
    wheel_a, manifest_a_path, manifest_a = artifact(tmp_path, "a")
    wheel_b, manifest_b_path, manifest_b = artifact(tmp_path, "b")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel_a, manifest_a_path, root=root, bin_dir=bin_dir)
        install_release(wheel_b, manifest_b_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state_before = layout.state_path.read_bytes()
    bootstrap_before = layout.bootstrap_path.read_bytes()
    launcher_before = layout.launcher_path.read_bytes()
    python = Path(str(load_managed_state(layout)["python_executable"]))
    target = target_activation_bundle(layout, python)
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
            raise OSError(f"simulated managed rollback {failure_target} failure")
        real_atomic(Path(path), content, mode=mode)

    verification = (
        mock.patch(
            "termdeck.release_manager._verify_managed_activation",
            side_effect=ReleaseError("simulated managed rollback verification failure"),
        )
        if failure_target == "verify"
        else nullcontext()
    )
    with mock.patch(
        "termdeck.release_manager._derive_release_activation_bundle",
        return_value=target,
    ), mock.patch(
        "termdeck.release_manager._atomic_write_bytes",
        side_effect=fail_once,
    ), verification, pytest.raises(ReleaseError, match="rolled back"):
        rollback_release(
            root=root,
            bin_dir=bin_dir,
            release_id=str(manifest_a["release_id"]),
        )

    assert layout.state_path.read_bytes() == state_before
    assert layout.bootstrap_path.read_bytes() == bootstrap_before
    assert layout.launcher_path.read_bytes() == launcher_before
    state = load_managed_state(layout)
    assert state["current_release_id"] == manifest_b["release_id"]
    assert state["previous_release_id"] == manifest_a["release_id"]


def test_managed_activation_accepts_only_exact_selected_release_bundle(
    tmp_path: Path,
) -> None:
    wheel, manifest_path, _manifest = artifact(tmp_path, "a")
    root = tmp_path / "managed"
    bin_dir = tmp_path / "bin"
    with mock.patch("termdeck.release_manager._verify_staged_import"):
        install_release(wheel, manifest_path, root=root, bin_dir=bin_dir)
    layout = release_layout(root=root, bin_dir=bin_dir)
    state = load_managed_state(layout)
    python = Path(str(state["python_executable"]))
    target = target_activation_bundle(layout, python)
    layout.bootstrap_path.write_bytes(target.bootstrap)
    layout.bootstrap_path.chmod(0o755)
    layout.launcher_path.write_bytes(target.launcher)
    layout.launcher_path.chmod(0o755)
    state["bootstrap_sha256"] = target.bootstrap_sha256
    state["launcher_sha256"] = target.launcher_sha256
    layout.state_path.write_bytes(canonical_json_bytes(validate_managed_state(state)))

    with mock.patch(
        "termdeck.release_manager._verified_release_activation_bundle",
        return_value=target,
    ):
        accepted = managed_release_status(root=root, bin_dir=bin_dir, verify=True)
    assert accepted["ok"] is True
    assert accepted["activation"]["activation_origin"] == "selected-release"

    wrong = target_activation_bundle(layout, python, label="wrong")
    with mock.patch(
        "termdeck.release_manager._verified_release_activation_bundle",
        return_value=wrong,
    ):
        rejected = managed_release_status(root=root, bin_dir=bin_dir, verify=True)
    assert rejected["ok"] is False
    assert "canonical bootstrap" in str(rejected["error"])


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


def test_process_observer_receipt_requires_complete_stable_samples(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        42,
        argv=prefix,
        executable=python,
        start_ticks=99,
    )
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    activation = {"current_release_id": state["current_release_id"], "legacy": False}

    with mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        return_value=activation,
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=[complete, copy.deepcopy(complete)],
    ):
        receipt = release_manager.managed_gui_process_observer_receipt(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert receipt["ok"] is True
    assert receipt["error_code"] == ""
    assert receipt["stable"] is True
    assert receipt["state_stable"] is True
    assert receipt["stable_samples"] == 2
    assert receipt["status"] == complete
    assert receipt["managed_state_sha256"] == sha256_bytes(
        canonical_json_bytes(state)
    )

    unstable = copy.deepcopy(complete)
    unstable["processes"][0]["process_start_ticks"] += 1
    with mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        return_value=activation,
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=[complete, unstable],
    ):
        receipt = release_manager.managed_gui_process_observer_receipt(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "process-scan-unstable"

    incomplete = copy.deepcopy(complete)
    incomplete.update(
        {
            "complete": False,
            "posture": "scan-incomplete",
            "activation": {
                "safe": False,
                "decision": "refuse-incomplete",
                "requires_shutdown": False,
            },
            "issues": [{"code": "same-uid-candidate-unreadable", "count": 1}],
        }
    )
    with mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        return_value=activation,
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=[incomplete, copy.deepcopy(incomplete)],
    ):
        receipt = release_manager.managed_gui_process_observer_receipt(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "process-scan-incomplete"


def test_process_observer_receipt_detects_managed_state_change(
    tmp_path: Path,
) -> None:
    root, bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    changed = copy.deepcopy(state)
    changed["generation"] = int(changed["generation"]) + 1
    with mock.patch.object(
        release_manager,
        "load_managed_state",
        side_effect=[state, changed],
    ), mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        return_value={"current_release_id": state["current_release_id"]},
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=[complete, copy.deepcopy(complete)],
    ):
        receipt = release_manager.managed_gui_process_observer_receipt(
            root=root,
            bin_dir=bin_dir,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert receipt["ok"] is False
    assert receipt["state_stable"] is False
    assert receipt["error_code"] == "managed-state-changed"


def test_process_observer_receipt_validation_accepts_exact_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        42,
        argv=prefix,
        executable=python,
        start_ticks=99,
    )
    status = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    source_module = Path(release_manager.__file__).resolve(strict=True)
    valid = process_observer_receipt(
        layout=layout,
        state=state,
        status=status,
        expected_uid=uid,
        source_module=source_module,
    )
    assert release_manager._validate_process_observer_receipt(
        valid,
        layout=layout,
        state=state,
        expected_uid=uid,
        direct=status,
        source_module=source_module,
    ) == status

    tampered: list[dict[str, object]] = []
    for field, value in (
        ("managed_state_sha256", "0" * 64),
        ("current_release_id", "f" * 64),
        ("expected_uid", uid + 1),
        ("source_module", str(tmp_path / "wrong.py")),
        ("stable", False),
    ):
        candidate = copy.deepcopy(valid)
        candidate[field] = value
        tampered.append(candidate)
    incomplete = copy.deepcopy(valid)
    incomplete["status"]["complete"] = False
    tampered.append(incomplete)
    wrong_identity = copy.deepcopy(valid)
    wrong_identity["status"]["expected_identity"] = {"python_executable": "/wrong"}
    tampered.append(wrong_identity)
    wrong_boot = copy.deepcopy(valid)
    wrong_boot["status"]["boot_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    tampered.append(wrong_boot)
    omitted = copy.deepcopy(valid)
    omitted["status"]["processes"] = []
    tampered.append(omitted)
    recursive = copy.deepcopy(valid)
    recursive["status"]["observer"] = {"mode": "unexpected"}
    tampered.append(recursive)
    private = copy.deepcopy(valid)
    private["status"]["processes"][0]["argv"] = ["secret"]
    tampered.append(private)

    for candidate in tampered:
        with pytest.raises(ReleaseError, match="process observer"):
            release_manager._validate_process_observer_receipt(
                candidate,
                layout=layout,
                state=state,
                expected_uid=uid,
                direct=status,
                source_module=source_module,
            )


def test_process_observer_routes_only_incomplete_real_proc_scans(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    incomplete = copy.deepcopy(complete)
    incomplete.update(
        {
            "complete": False,
            "posture": "scan-incomplete",
            "processes": [],
            "gui_count": 0,
            "issues": [{"code": "same-uid-candidate-unreadable", "count": 1}],
        }
    )

    with mock.patch.object(
        release_manager, "_scan_managed_gui_processes", return_value=complete
    ), mock.patch.object(
        release_manager, "_scan_managed_gui_processes_via_user_manager"
    ) as broker:
        direct = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    assert direct["observer"] == {
        "mode": "direct",
        "direct_complete": True,
        "broker_attempted": False,
        "broker_result": "not-needed",
        "singleflight_role": "none",
        "broker_shared": False,
        "direct_singleflight_role": "leader",
        "direct_shared": False,
        "direct_scan_result": "accepted",
    }
    broker.assert_not_called()

    with mock.patch.object(
        release_manager, "_scan_managed_gui_processes", return_value=incomplete
    ), mock.patch.object(
        release_manager, "_canonical_real_proc_root", return_value=True
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes_via_user_manager",
        return_value=(
            complete,
            "accepted",
            {"singleflight_role": "leader", "broker_shared": False},
        ),
    ) as broker:
        observed = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=Path("/proc"),
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    assert observed["complete"] is True
    assert observed["observer"]["mode"] == "systemd-user-manager"
    assert observed["observer"]["broker_result"] == "accepted"
    broker.assert_called_once()

    with mock.patch.object(
        release_manager, "_scan_managed_gui_processes", return_value=incomplete
    ), mock.patch.object(
        release_manager, "_canonical_real_proc_root", return_value=True
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes_via_user_manager",
        return_value=(
            None,
            "broker-receipt-rejected",
            {"singleflight_role": "follower", "broker_shared": True},
        ),
    ):
        failed = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=Path("/proc"),
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    assert failed["complete"] is False
    assert failed["observer"]["mode"] == "direct"
    assert failed["observer"]["broker_result"] == "broker-receipt-rejected"

    with mock.patch.object(
        release_manager, "_scan_managed_gui_processes", return_value=incomplete
    ), mock.patch.object(
        release_manager, "_scan_managed_gui_processes_via_user_manager"
    ) as broker:
        fake = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    assert fake["complete"] is False
    broker.assert_not_called()

    with mock.patch.object(
        release_manager, "_scan_managed_gui_processes", return_value=incomplete
    ), mock.patch.object(
        release_manager, "_canonical_real_proc_root", return_value=True
    ), mock.patch.dict(
        os.environ, {release_manager._PROCESS_OBSERVER_ENV: "1"}
    ), mock.patch.object(
        release_manager, "_scan_managed_gui_processes_via_user_manager"
    ) as broker:
        recursive = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=Path("/proc"),
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    assert recursive["complete"] is False
    broker.assert_not_called()


def test_user_manager_process_observer_dispatch_is_bounded_and_revalidated(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    direct = copy.deepcopy(complete)
    direct.update(
        {
            "complete": False,
            "posture": "scan-incomplete",
            "processes": [],
            "gui_count": 0,
            "issues": [{"code": "same-uid-candidate-unreadable", "count": 1}],
        }
    )
    payload = process_observer_receipt(
        layout=layout,
        state=state,
        status=complete,
        expected_uid=uid,
    )
    activation = {"current_release_id": state["current_release_id"], "legacy": False}
    completed = release_manager._BoundedProcessResult(
        0,
        canonical_json_bytes(payload),
        b"",
    )
    with mock.patch.object(
        release_manager, "_verify_managed_activation", return_value=activation
    ) as verify, mock.patch.object(
        release_manager, "_trusted_systemd_run", return_value=Path("/usr/bin/systemd-run")
    ), mock.patch.object(
        release_manager, "_run_bounded_output", return_value=completed
    ) as run:
        observed, result, flight = release_manager._scan_managed_gui_processes_via_user_manager(
            layout,
            state,
            expected_uid=uid,
            direct=direct,
        )
    assert result == "accepted"
    assert observed == complete
    assert flight == {
        "singleflight_role": "leader",
        "broker_shared": False,
        "cross_process_role": "leader",
        "cross_process_shared": False,
        "cross_process_result": "published",
    }
    # Two transport checks, one pre-publication redaction/identity validation,
    # and one caller-local validation after consuming the shared bytes.
    assert verify.call_count == 4
    command = run.call_args.args[0]
    assert command[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--wait",
        "--collect",
        "--pipe",
    ]
    assert "--quiet" in command
    assert any(item.startswith("--property=RuntimeMaxSec=") for item in command)
    assert "--property=TimeoutStopSec=2s" in command
    assert f"--setenv={release_manager._PROCESS_OBSERVER_ENV}=1" in command
    python_index = command.index(str(Path(sys.executable).resolve(strict=True)))
    assert command[python_index + 1 : python_index + 4] == ["-I", "-B", "-c"]
    assert command[python_index + 4] == release_manager._PROCESS_OBSERVER_HELPER_SOURCE
    assert command[-5:] == [
        str(Path(release_manager.__file__).resolve(strict=True).parent.parent),
        str(Path(release_manager.__file__).resolve(strict=True)),
        str(layout.root),
        str(layout.bin_dir),
        str(uid),
    ]
    assert run.call_args.kwargs["timeout"] == pytest.approx(
        release_manager._PROCESS_OBSERVER_TIMEOUT_SECONDS + 2.0
    )
    client_environment = run.call_args.kwargs["env"]
    assert "PYTHONPATH" not in client_environment
    assert client_environment["HOME"] == str(Path.home().resolve(strict=True))
    assert client_environment["XDG_RUNTIME_DIR"] == f"/run/user/{uid}"
    assert client_environment["DBUS_SESSION_BUS_ADDRESS"] == (
        f"unix:path=/run/user/{uid}/bus"
    )
    assert client_environment["LANG"] == client_environment["LC_ALL"] == "C.UTF-8"
    assert client_environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert client_environment["PYTHONHASHSEED"] == "0"


def test_user_manager_process_observer_rejects_bad_execution_and_receipts(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    direct = copy.deepcopy(complete)
    direct.update({"complete": False, "processes": [], "gui_count": 0})
    valid = process_observer_receipt(
        layout=layout,
        state=state,
        status=complete,
        expected_uid=uid,
    )
    tampered = copy.deepcopy(valid)
    tampered["managed_state_sha256"] = "0" * 64
    cases = (
        (release_manager._BoundedProcessResult(7, b"", b""), "broker-command-failed"),
        (release_manager._BoundedProcessResult(0, b"{}\n", b"warning"), "broker-stderr-not-empty"),
        (release_manager._BoundedProcessResult(0, b"not-json", b""), "broker-output-invalid"),
        (
            release_manager._BoundedProcessResult(
                0, canonical_json_bytes(tampered), b""
            ),
            "broker-receipt-rejected",
        ),
    )
    activation = {"current_release_id": state["current_release_id"], "legacy": False}
    for completed, expected in cases:
        with mock.patch.object(
            release_manager, "_verify_managed_activation", return_value=activation
        ), mock.patch.object(
            release_manager,
            "_trusted_systemd_run",
            return_value=Path("/usr/bin/systemd-run"),
        ), mock.patch.object(
            release_manager, "_run_bounded_output", return_value=completed
        ):
            observed, result, flight = release_manager._scan_managed_gui_processes_via_user_manager(
                layout,
                state,
                expected_uid=uid,
                direct=direct,
            )
        assert observed is None
        assert result == expected
        assert flight == {
        "singleflight_role": "leader",
        "broker_shared": False,
        "cross_process_role": "leader",
        "cross_process_shared": False,
        "cross_process_result": "published",
    }

    with mock.patch.object(
        release_manager, "_verify_managed_activation", return_value=activation
    ), mock.patch.object(
        release_manager, "_trusted_systemd_run", return_value=Path("/usr/bin/systemd-run")
    ), mock.patch.object(
        release_manager,
        "_run_bounded_output",
        side_effect=ReleaseError("timed out"),
    ):
        observed, result, flight = release_manager._scan_managed_gui_processes_via_user_manager(
            layout,
            state,
            expected_uid=uid,
            direct=direct,
        )
    assert observed is None
    assert result == "broker-dispatch-failed"
    assert flight == {
        "singleflight_role": "leader",
        "broker_shared": False,
        "cross_process_role": "leader",
        "cross_process_shared": False,
        "cross_process_result": "published",
    }



def test_process_observer_flight_key_binds_all_shared_assumptions(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        42,
        argv=prefix,
        executable=python,
        start_ticks=99,
    )
    direct = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    source_module = Path(release_manager.__file__).resolve(strict=True)
    base = release_manager._process_observer_flight_key(
        layout,
        state,
        expected_uid=uid,
        direct=direct,
        source_module=source_module,
    )

    different_process_subset = copy.deepcopy(direct)
    different_process_subset["processes"] = []
    assert release_manager._process_observer_flight_key(
        layout,
        state,
        expected_uid=uid,
        direct=different_process_subset,
        source_module=source_module,
    ) == base

    changed_state = copy.deepcopy(state)
    changed_state["generation"] = int(changed_state["generation"]) + 1
    changed_boot = copy.deepcopy(direct)
    changed_boot["boot_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    changed_identity = copy.deepcopy(direct)
    changed_identity["expected_identity"] = {
        **dict(changed_identity["expected_identity"]),
        "python_executable": "/different/python",
    }
    alternate_layout = release_layout(
        root=tmp_path / "other-managed",
        bin_dir=tmp_path / "other-bin",
    )
    alternate_source = tmp_path / "other-release-manager.py"
    alternate_source.write_text("# alternate source\n", encoding="utf-8")
    variants = (
        release_manager._process_observer_flight_key(
            layout,
            changed_state,
            expected_uid=uid,
            direct=direct,
            source_module=source_module,
        ),
        release_manager._process_observer_flight_key(
            layout,
            state,
            expected_uid=uid + 1,
            direct=direct,
            source_module=source_module,
        ),
        release_manager._process_observer_flight_key(
            layout,
            state,
            expected_uid=uid,
            direct=changed_boot,
            source_module=source_module,
        ),
        release_manager._process_observer_flight_key(
            layout,
            state,
            expected_uid=uid,
            direct=changed_identity,
            source_module=source_module,
        ),
        release_manager._process_observer_flight_key(
            alternate_layout,
            state,
            expected_uid=uid,
            direct=direct,
            source_module=source_module,
        ),
        release_manager._process_observer_flight_key(
            layout,
            state,
            expected_uid=uid,
            direct=direct,
            source_module=alternate_source,
        ),
    )
    assert all(candidate != base for candidate in variants)


def test_process_observer_singleflight_coalesces_only_inflight_requests() -> None:
    key = ("same-observation",)
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    dispatch_count = 0

    def dispatch() -> release_manager._ProcessObserverTransportResult:
        nonlocal dispatch_count
        with counter_lock:
            dispatch_count += 1
        entered.set()
        assert release.wait(_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS)
        return release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"{}\n",
        )

    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        leader = executor.submit(
            release_manager._process_observer_transport_singleflight,
            key,
            dispatch,
        )
        assert entered.wait(_TEST_CONCURRENCY_START_TIMEOUT_SECONDS)
        followers = [
            executor.submit(
                release_manager._process_observer_transport_singleflight,
                key,
                dispatch,
            )
            for _ in range(15)
        ]
        deadline = time.monotonic() + _TEST_CONCURRENCY_START_TIMEOUT_SECONDS
        while not all(future.running() for future in followers):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        time.sleep(_TEST_CONCURRENCY_SETTLE_SECONDS)
        release.set()
        results = [leader.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS)] + [
            future.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS) for future in followers
        ]

    assert dispatch_count == 1
    roles = [metadata["singleflight_role"] for _result, metadata in results]
    assert roles.count("leader") == 1
    assert roles.count("follower") == 15
    assert sum(bool(metadata["broker_shared"]) for _result, metadata in results) == 15
    assert all(result.result_code == "broker-output-ready" for result, _metadata in results)
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS

    def fresh_dispatch() -> release_manager._ProcessObserverTransportResult:
        nonlocal dispatch_count
        with counter_lock:
            dispatch_count += 1
        return release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"{}\n",
        )

    for _ in range(2):
        result, metadata = release_manager._process_observer_transport_singleflight(
            key,
            dispatch=fresh_dispatch,
        )
        assert result.result_code == "broker-output-ready"
        assert metadata == {"singleflight_role": "leader", "broker_shared": False}
    # The first concurrent interval used one dispatch; each later non-overlapping
    # call was a fresh leader rather than a completed-result cache hit.
    assert dispatch_count == 3


def test_process_observer_singleflight_does_not_merge_different_keys() -> None:
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls: list[str] = []

    def dispatch(label: str) -> release_manager._ProcessObserverTransportResult:
        with counter_lock:
            calls.append(label)
        barrier.wait(timeout=_TEST_CONCURRENCY_BARRIER_TIMEOUT_SECONDS)
        return release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            label.encode("ascii"),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            release_manager._process_observer_transport_singleflight,
            ("one",),
            lambda: dispatch("one"),
        )
        second = executor.submit(
            release_manager._process_observer_transport_singleflight,
            ("two",),
            lambda: dispatch("two"),
        )
        results = (first.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS), second.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS))
    assert sorted(calls) == ["one", "two"]
    assert all(
        metadata == {"singleflight_role": "leader", "broker_shared": False}
        for _result, metadata in results
    )
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS


def test_process_observer_singleflight_follower_timeout_is_fail_closed() -> None:
    key = ("stalled-leader",)
    flight = release_manager._ProcessObserverFlight()
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS
        release_manager._PROCESS_OBSERVER_FLIGHTS[key] = flight
    dispatch = mock.Mock(side_effect=AssertionError("follower dispatched unexpectedly"))
    try:
        with mock.patch.object(
            release_manager,
            "_PROCESS_OBSERVER_FOLLOWER_WAIT_SECONDS",
            0.01,
        ):
            result, metadata = release_manager._process_observer_transport_singleflight(
                key,
                dispatch,
            )
        assert result.result_code == "broker-singleflight-timeout"
        assert metadata == {"singleflight_role": "follower", "broker_shared": True}
        dispatch.assert_not_called()
    finally:
        with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
            release_manager._PROCESS_OBSERVER_FLIGHTS.pop(key, None)


def test_process_observer_singleflight_leader_exception_releases_followers() -> None:
    key = ("leader-exception",)
    entered = threading.Event()
    release = threading.Event()

    def dispatch() -> release_manager._ProcessObserverTransportResult:
        entered.set()
        assert release.wait(_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS)
        raise RuntimeError("unexpected observer failure")

    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        leader = executor.submit(
            release_manager._process_observer_transport_singleflight,
            key,
            dispatch,
        )
        assert entered.wait(_TEST_CONCURRENCY_START_TIMEOUT_SECONDS)
        followers = [
            executor.submit(
                release_manager._process_observer_transport_singleflight,
                key,
                dispatch,
            )
            for _ in range(4)
        ]
        deadline = time.monotonic() + _TEST_CONCURRENCY_START_TIMEOUT_SECONDS
        while not all(future.running() for future in followers):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        time.sleep(_TEST_CONCURRENCY_SETTLE_SECONDS)
        release.set()
        with pytest.raises(RuntimeError, match="unexpected observer failure"):
            leader.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS)
        follower_results = [future.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS) for future in followers]

    assert all(
        result.result_code == "broker-leader-exception"
        and metadata == {"singleflight_role": "follower", "broker_shared": True}
        for result, metadata in follower_results
    )
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS


def test_shared_process_observer_transport_keeps_caller_specific_validation(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    python = Path(sys.executable).resolve(strict=True)
    bootstrap = layout.bootstrap_path.resolve(strict=True)
    prefix = (os.fsencode(str(python)), b"-I", os.fsencode(str(bootstrap)))
    write_fake_proc_process(
        proc_root,
        42,
        argv=prefix,
        executable=python,
        start_ticks=99,
    )
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    direct_empty = copy.deepcopy(complete)
    direct_empty.update(
        {
            "complete": False,
            "posture": "scan-incomplete",
            "processes": [],
            "gui_count": 0,
            "issues": [{"code": "same-uid-candidate-unreadable", "count": 1}],
        }
    )
    direct_subset = copy.deepcopy(complete)
    direct_subset.update(
        {
            "complete": False,
            "posture": "scan-incomplete",
            "issues": [{"code": "same-uid-candidate-unreadable", "count": 1}],
        }
    )
    payload = process_observer_receipt(
        layout=layout,
        state=state,
        status=complete,
        expected_uid=uid,
    )
    transport = release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        canonical_json_bytes(payload),
    )
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    validation_lock = threading.Lock()
    dispatch_count = 0
    validated_subset_sizes: list[int] = []
    original_validate = release_manager._validate_process_observer_receipt

    def dispatch(*_args, **_kwargs) -> release_manager._ProcessObserverTransportResult:
        nonlocal dispatch_count
        with counter_lock:
            dispatch_count += 1
        entered.set()
        assert release.wait(_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS)
        return transport

    def validate(payload_value, **kwargs):
        with validation_lock:
            validated_subset_sizes.append(len(kwargs["direct"]["processes"]))
        return original_validate(payload_value, **kwargs)

    activation = {"current_release_id": state["current_release_id"], "legacy": False}
    with mock.patch.object(
        release_manager,
        "_dispatch_process_observer_transport",
        side_effect=dispatch,
    ), mock.patch.object(
        release_manager,
        "_verify_managed_activation",
        return_value=activation,
    ), mock.patch.object(
        release_manager,
        "_validate_process_observer_receipt",
        side_effect=validate,
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(
                release_manager._scan_managed_gui_processes_via_user_manager,
                layout,
                state,
                expected_uid=uid,
                direct=direct_empty,
            )
            assert entered.wait(_TEST_CONCURRENCY_START_TIMEOUT_SECONDS)
            follower = executor.submit(
                release_manager._scan_managed_gui_processes_via_user_manager,
                layout,
                state,
                expected_uid=uid,
                direct=direct_subset,
            )
            deadline = time.monotonic() + _TEST_CONCURRENCY_START_TIMEOUT_SECONDS
            while not follower.running():
                assert time.monotonic() < deadline
                time.sleep(0.001)
            time.sleep(_TEST_CONCURRENCY_SETTLE_SECONDS)
            release.set()
            results = (leader.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS), follower.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS))

    assert dispatch_count == 1
    # The leader validates its empty direct subset before publication and again
    # as a caller; the follower independently validates its one-process subset.
    assert sorted(validated_subset_sizes) == [0, 0, 1]
    assert all(status == complete and result == "accepted" for status, result, _meta in results)
    roles = sorted(meta["singleflight_role"] for _status, _result, meta in results)
    assert roles == ["follower", "leader"]
    assert sum(bool(meta["broker_shared"]) for _status, _result, meta in results) == 1
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS


def test_shared_process_observer_follower_revalidates_current_state(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    complete = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    direct = copy.deepcopy(complete)
    direct.update({"complete": False, "processes": [], "gui_count": 0})
    payload = process_observer_receipt(
        layout=layout,
        state=state,
        status=complete,
        expected_uid=uid,
    )
    changed = copy.deepcopy(state)
    changed["generation"] = int(changed["generation"]) + 1
    transport = release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        canonical_json_bytes(payload),
    )
    with mock.patch.object(
        release_manager,
        "_process_observer_transport_singleflight",
        return_value=(
            transport,
            {"singleflight_role": "follower", "broker_shared": True},
        ),
    ), mock.patch.object(
        release_manager,
        "load_managed_state",
        return_value=changed,
    ):
        observed, result, metadata = (
            release_manager._scan_managed_gui_processes_via_user_manager(
                layout,
                state,
                expected_uid=uid,
                direct=direct,
            )
        )
    assert observed is None
    assert result == "broker-receipt-rejected"
    assert metadata == {
        "singleflight_role": "follower",
        "broker_shared": True,
        "cross_process_role": "none",
        "cross_process_shared": False,
        "cross_process_result": "not-attempted",
    }



def test_direct_process_scan_flight_key_binds_namespace_state_and_source(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    source_module = Path(release_manager.__file__).resolve(strict=True)
    base = release_manager._direct_process_scan_flight_key(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
        source_module=source_module,
    )

    changed_state = copy.deepcopy(state)
    changed_state["generation"] = int(changed_state["generation"]) + 1
    other_proc = tmp_path / "other-proc"
    other_proc.mkdir()
    other_layout = release_layout(
        root=tmp_path / "other-managed",
        bin_dir=tmp_path / "other-bin",
    )
    variants = (
        release_manager._direct_process_scan_flight_key(
            layout,
            changed_state,
            proc_root=proc_root,
            expected_uid=uid,
            source_module=source_module,
        ),
        release_manager._direct_process_scan_flight_key(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid + 1,
            source_module=source_module,
        ),
        release_manager._direct_process_scan_flight_key(
            layout,
            state,
            proc_root=other_proc,
            expected_uid=uid,
            source_module=source_module,
        ),
        release_manager._direct_process_scan_flight_key(
            other_layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
            source_module=source_module,
        ),
        release_manager._direct_process_scan_flight_key(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
            source_module=tmp_path / "other-release-manager.py",
        ),
    )
    assert all(candidate != base for candidate in variants)


def test_direct_process_scan_singleflight_coalesces_and_reparses(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    status = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def scan(*_args, **_kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
        entered.set()
        assert release.wait(_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS)
        return copy.deepcopy(status)

    def call():
        return release_manager._scan_managed_gui_processes_singleflight(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
        )

    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS
    with mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=scan,
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            leader = executor.submit(call)
            assert entered.wait(_TEST_CONCURRENCY_START_TIMEOUT_SECONDS)
            followers = [executor.submit(call) for _ in range(15)]
            deadline = time.monotonic() + _TEST_CONCURRENCY_START_TIMEOUT_SECONDS
            while not all(future.running() for future in followers):
                assert time.monotonic() < deadline
                time.sleep(0.001)
            time.sleep(_TEST_CONCURRENCY_SETTLE_SECONDS)
            release.set()
            results = [leader.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS)] + [
                future.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS) for future in followers
            ]

    assert calls == 1
    roles = [metadata["direct_singleflight_role"] for _result, metadata in results]
    assert roles.count("leader") == 1
    assert roles.count("follower") == 15
    assert sum(bool(metadata["direct_shared"]) for _result, metadata in results) == 15
    assert all(metadata["direct_scan_result"] == "accepted" for _result, metadata in results)
    payloads = [result for result, _metadata in results]
    assert all(result == status for result in payloads)
    assert len({id(result) for result in payloads}) == 16
    assert len({id(result["processes"]) for result in payloads}) == 16
    assert len({id(result["issues"]) for result in payloads}) == 16
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS

    sequential_calls = 0

    def sequential_scan(*_args, **_kwargs):
        nonlocal sequential_calls
        sequential_calls += 1
        return copy.deepcopy(status)

    with mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=sequential_scan,
    ):
        first, first_meta = call()
        second, second_meta = call()
    assert sequential_calls == 2
    assert first == second == status
    assert first is not second
    assert first_meta == second_meta == {
        "direct_singleflight_role": "leader",
        "direct_shared": False,
        "direct_scan_result": "accepted",
    }


def test_direct_process_scan_singleflight_does_not_merge_different_keys(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    other_proc = tmp_path / "other-proc"
    other_proc.mkdir()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls: list[str] = []

    def scan(_layout, _state, *, proc_root: Path, expected_uid: int):
        del _layout, _state, expected_uid
        label = str(proc_root)
        with lock:
            calls.append(label)
        barrier.wait(timeout=_TEST_CONCURRENCY_BARRIER_TIMEOUT_SECONDS)
        return release_manager._direct_scan_failure_status(
            state,
            expected_uid=uid,
            issue_code="expected-test-result",
        )

    def call(root: Path):
        return release_manager._scan_managed_gui_processes_singleflight(
            layout,
            state,
            proc_root=root,
            expected_uid=uid,
        )

    with mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=scan,
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(call, proc_root)
            second = executor.submit(call, other_proc)
            results = (first.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS), second.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS))
    assert sorted(calls) == sorted([str(proc_root), str(other_proc)])
    assert all(
        metadata == {
            "direct_singleflight_role": "leader",
            "direct_shared": False,
            "direct_scan_result": "accepted",
        }
        for _result, metadata in results
    )
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS


def test_direct_process_scan_timeout_is_synthetic_and_does_not_rescan(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    source_module = Path(release_manager.__file__).resolve(strict=True)
    key = release_manager._direct_process_scan_flight_key(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
        source_module=source_module,
    )
    flight = release_manager._ProcessObserverFlight()
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS
        release_manager._PROCESS_OBSERVER_FLIGHTS[key] = flight
    try:
        with mock.patch.object(
            release_manager,
            "_PROCESS_DIRECT_SCAN_FOLLOWER_WAIT_SECONDS",
            0.01,
        ), mock.patch.object(
            release_manager,
            "_scan_managed_gui_processes",
        ) as scan:
            status, metadata = release_manager._scan_managed_gui_processes_singleflight(
                layout,
                state,
                proc_root=proc_root,
                expected_uid=uid,
            )
        scan.assert_not_called()
        assert status["complete"] is False
        assert status["posture"] == "scan-incomplete"
        assert status["activation"]["safe"] is False
        assert status["issues"] == [
            {"code": "direct-singleflight-timeout", "count": 1}
        ]
        assert metadata == {
            "direct_singleflight_role": "follower",
            "direct_shared": True,
            "direct_scan_result": "direct-singleflight-timeout",
        }
    finally:
        with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
            release_manager._PROCESS_OBSERVER_FLIGHTS.pop(key, None)


def test_direct_process_scan_failure_is_synthetic_and_registry_is_cleared(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    with mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes",
        side_effect=RuntimeError("procfs reader failed"),
    ):
        status, metadata = release_manager._scan_managed_gui_processes_singleflight(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert status["complete"] is False
    assert status["activation"] == {
        "safe": False,
        "decision": "refuse-incomplete",
        "requires_shutdown": False,
    }
    assert status["issues"] == [{"code": "direct-scan-failed", "count": 1}]
    assert metadata == {
        "direct_singleflight_role": "leader",
        "direct_shared": False,
        "direct_scan_result": "direct-scan-failed",
    }
    with release_manager._PROCESS_OBSERVER_FLIGHT_LOCK:
        assert not release_manager._PROCESS_OBSERVER_FLIGHTS


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"schema": "wrong-schema"}),
        lambda payload: payload.update({"expected_uid": int(payload["expected_uid"]) + 1}),
        lambda payload: payload.update({"argv": ["private"]}),
        lambda payload: payload.update({"gui_count": True}),
    ],
)
def test_direct_process_scan_rejects_invalid_shared_bytes(
    tmp_path: Path,
    mutation,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    valid = release_manager._scan_managed_gui_processes(
        layout,
        state,
        proc_root=proc_root,
        expected_uid=uid,
    )
    mutation(valid)
    transport = release_manager._ProcessObserverTransportResult(
        "direct-scan-output-ready",
        canonical_json_bytes(valid),
    )
    with mock.patch.object(
        release_manager,
        "_process_observer_transport_singleflight",
        return_value=(
            transport,
            {"singleflight_role": "follower", "broker_shared": True},
        ),
    ):
        status, metadata = release_manager._scan_managed_gui_processes_singleflight(
            layout,
            state,
            proc_root=proc_root,
            expected_uid=uid,
        )
    assert status["complete"] is False
    assert status["issues"] == [
        {"code": "direct-scan-output-invalid", "count": 1}
    ]
    assert metadata == {
        "direct_singleflight_role": "follower",
        "direct_shared": True,
        "direct_scan_result": "direct-scan-output-invalid",
    }


def test_observer_never_brokers_after_synthetic_direct_failure(
    tmp_path: Path,
) -> None:
    _root, _bin_dir, layout, state, proc_root = managed_process_fixture(tmp_path)
    uid = os.getuid()
    synthetic = release_manager._direct_scan_failure_status(
        state,
        expected_uid=uid,
        issue_code="direct-singleflight-timeout",
    )
    metadata = {
        "direct_singleflight_role": "follower",
        "direct_shared": True,
        "direct_scan_result": "direct-singleflight-timeout",
    }
    with mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes_singleflight",
        return_value=(synthetic, metadata),
    ), mock.patch.object(
        release_manager,
        "_scan_managed_gui_processes_via_user_manager",
    ) as broker:
        observed = release_manager._observe_managed_gui_processes(
            layout,
            state,
            proc_root=Path("/proc"),
            expected_uid=uid,
            allow_user_manager_broker=True,
        )
    broker.assert_not_called()
    assert observed["complete"] is False
    assert observed["issues"] == [
        {"code": "direct-singleflight-timeout", "count": 1}
    ]
    assert observed["observer"]["direct_scan_result"] == (
        "direct-singleflight-timeout"
    )
    assert observed["observer"]["broker_attempted"] is False



def test_cross_process_flight_publication_is_private_and_start_bound(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "cross-runtime"
    root = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=runtime,
    )
    assert root == runtime.absolute()
    metadata = root.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    assert metadata.st_uid == uid

    key = ("cross-publication", "v2")
    concurrent_start = time.monotonic_ns()
    leader = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
        request_started_monotonic_ns=concurrent_start,
    )
    follower = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
        request_started_monotonic_ns=concurrent_start,
    )
    assert leader.role == "leader"
    assert follower.role == "follower"
    assert follower.nonce == leader.nonce
    assert follower.result_path == leader.result_path
    assert follower.leader_pid == leader.leader_pid == os.getpid()
    assert follower.leader_start_ticks == leader.leader_start_ticks
    for protocol_file in (leader.state_path, leader.lock_path):
        observed = protocol_file.lstat()
        assert stat.S_ISREG(observed.st_mode)
        assert stat.S_IMODE(observed.st_mode) == 0o600
        assert observed.st_uid == uid
        assert observed.st_nlink == 1

    raw = b'{"redaction_safe":true}\n'
    published = release_manager._publish_cross_result(
        leader,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            raw,
        ),
        expected_uid=uid,
    )
    assert published == release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        raw,
    )
    state = release_manager._read_cross_json(
        leader.state_path,
        expected_uid=uid,
        max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
    )
    assert state is not None
    assert state["phase"] == "published"
    published_ns = int(state["published_monotonic_ns"])
    assert concurrent_start <= published_ns
    result_metadata = leader.result_path.lstat()
    assert stat.S_ISREG(result_metadata.st_mode)
    assert stat.S_IMODE(result_metadata.st_mode) == 0o600
    assert result_metadata.st_uid == uid
    assert result_metadata.st_nlink == 1
    assert release_manager._wait_cross_result(
        follower,
        expected_uid=uid,
    ) == published

    late_concurrent = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
        request_started_monotonic_ns=concurrent_start,
    )
    assert late_concurrent.role == "follower"
    assert late_concurrent.nonce == leader.nonce
    assert release_manager._wait_cross_result(
        late_concurrent,
        expected_uid=uid,
    ) == published

    while time.monotonic_ns() <= published_ns:
        pass
    post_publication_start = time.monotonic_ns()
    fresh = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
        request_started_monotonic_ns=post_publication_start,
    )
    assert fresh.role == "leader"
    assert fresh.nonce != leader.nonce
    assert fresh.result_path != leader.result_path
    # The fresh post-publication leader replaces only admission state. A
    # previously admitted follower can still consume the old nonce-bound result.
    assert release_manager._wait_cross_result(
        late_concurrent,
        expected_uid=uid,
    ) == published
    release_manager._clear_matching_cross_state(fresh, expected_uid=uid)


def test_cross_process_follower_uses_fresh_clock_after_state_read(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "fresh-clock-after-state-read",
    )
    key = ("fresh-clock-after-state-read",)
    key_sha256 = sha256_bytes(canonical_json_bytes(key))
    nonce = "a" * 32
    result_name = f"result-{key_sha256}-{nonce}.json"
    start_ticks = release_manager._process_start_time(os.getpid())
    assert start_ticks is not None
    state_path = runtime / f"flight-{key_sha256}.json"
    release_manager._atomic_write_bytes(
        state_path,
        canonical_json_bytes(
            {
                "schema": release_manager._PROCESS_CROSS_STATE_SCHEMA,
                "key_sha256": key_sha256,
                "nonce": nonce,
                "result_name": result_name,
                "leader_pid": os.getpid(),
                "leader_start_ticks": start_ticks,
                "boot_id": release_manager._read_boot_id(Path("/proc")),
                "started_monotonic_ns": 100,
                "phase": "running",
                "published_monotonic_ns": 0,
            }
        ),
        mode=0o600,
    )

    # The caller samples 90 before waiting for the registry lock. The live
    # leader publishes its state at 100 before the follower reads it. Admission
    # must refresh the clock after reading state and validate with 110; using
    # the stale 90 sample incorrectly gives a negative age and creates a second
    # leader.
    with mock.patch.object(
        release_manager,
        "_cleanup_cross_results",
    ), mock.patch.object(
        release_manager.time,
        "monotonic_ns",
        side_effect=[90, 110],
    ):
        follower = release_manager._admit_cross_process_flight(
            key,
            expected_uid=uid,
            runtime_root=runtime,
        )

    assert follower.role == "follower"
    assert follower.nonce == nonce
    assert follower.leader_pid == os.getpid()
    assert follower.leader_start_ticks == start_ticks


def test_cross_process_published_result_does_not_require_live_publisher(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "published-dead-leader"
    key = ("published-dead-leader",)
    concurrent_start = time.monotonic_ns()
    leader = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
        request_started_monotonic_ns=concurrent_start,
    )
    published = release_manager._publish_cross_result(
        leader,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"published\n",
        ),
        expected_uid=uid,
    )
    with mock.patch.object(
        release_manager,
        "_process_start_time",
        return_value=None,
    ):
        follower = release_manager._admit_cross_process_flight(
            key,
            expected_uid=uid,
            runtime_root=runtime,
            request_started_monotonic_ns=concurrent_start,
        )
    assert follower.role == "follower"
    assert follower.nonce == leader.nonce
    assert release_manager._wait_cross_result(
        follower,
        expected_uid=uid,
    ) == published


@pytest.mark.parametrize("request_start", [True, -1, "123", 1.5])
def test_cross_process_request_start_rejects_invalid_values(
    tmp_path: Path,
    request_start: object,
) -> None:
    uid = os.getuid()
    with pytest.raises(ReleaseError, match="request start"):
        release_manager._admit_cross_process_flight(
            ("invalid-request-start", repr(request_start)),
            expected_uid=uid,
            runtime_root=tmp_path / repr(request_start),
            request_started_monotonic_ns=request_start,
        )



def test_cross_process_request_start_rejects_future_value(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    with pytest.raises(ReleaseError, match="request start"):
        release_manager._admit_cross_process_flight(
            ("future-request-start",),
            expected_uid=uid,
            runtime_root=tmp_path / "future",
            request_started_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        )


def test_cross_process_published_state_cleanup_is_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "published-state-cleanup"
    leader = release_manager._admit_cross_process_flight(
        ("published-state-cleanup",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    release_manager._publish_cross_result(
        leader,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"cleanup\n",
        ),
        expected_uid=uid,
    )
    assert leader.state_path.exists()
    assert leader.result_path.exists()
    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_LATE_JOIN_TTL_NS",
        0,
    ):
        time.sleep(0.001)
        release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=leader.boot_id,
        )
    assert not leader.state_path.exists()
    # Result retention is independently longer so already-admitted followers
    # are not stranded when the late-join marker expires.
    assert leader.result_path.exists()


def test_cross_process_distinct_keys_use_one_bounded_registry_lock(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "registry-cardinality"
    key_count = 64
    # This test measures registry-lock cardinality, not expiry. Freeze both
    # publication horizons so a heavily I/O-throttled test host cannot retire
    # early keys while the 64-key sequence is still being constructed.
    retention_ns = 60 * 60 * 1_000_000_000
    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_LATE_JOIN_TTL_NS",
        retention_ns,
    ), mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_RESULT_TTL_NS",
        retention_ns,
    ):
        for index in range(key_count):
            observed = release_manager._process_observer_cross_process_singleflight(
                ("registry-cardinality", str(index)),
                expected_uid=uid,
                runtime_root=runtime,
                dispatch=lambda index=index: release_manager._ProcessObserverTransportResult(
                    "broker-output-ready",
                    f'{{"index":{index}}}\n'.encode("ascii"),
                ),
            )
            assert observed.result_code == "broker-output-ready"
            assert observed.cross_process_role == "leader"
            assert observed.cross_process_shared is False

    lock_names = sorted(path.name for path in runtime.glob("*.lock"))
    legacy_lock_names = [
        name
        for name in lock_names
        if name.startswith("flight-") and name.endswith(".lock")
    ]
    assert lock_names == ["registry.lock"]
    assert legacy_lock_names == []
    state_paths = sorted(runtime.glob("flight-*.json"))
    assert len(state_paths) == key_count
    for state_path in state_paths:
        state = release_manager._read_cross_json(
            state_path,
            expected_uid=uid,
            max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
        )
        assert state is not None
        assert state["phase"] == "published"
        assert int(state["published_monotonic_ns"]) > 0



def test_cross_process_registry_lock_preserves_legacy_locks_as_inert(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "legacy-locks",
    )
    legacy = runtime / f"flight-{'a' * 64}.lock"
    legacy.write_bytes(b"legacy-lock-inode\n")
    legacy.chmod(0o600)
    before = legacy.lstat()
    before_sha256 = sha256_file(legacy)

    for index in range(4):
        observed = release_manager._process_observer_cross_process_singleflight(
            ("legacy-lock-preservation", str(index)),
            expected_uid=uid,
            runtime_root=runtime,
            dispatch=lambda index=index: release_manager._ProcessObserverTransportResult(
                "broker-output-ready",
                f'{{"legacy_probe":{index}}}\n'.encode("ascii"),
            ),
        )
        assert observed.result_code == "broker-output-ready"
        assert observed.cross_process_role == "leader"

    after = legacy.lstat()
    assert (after.st_dev, after.st_ino, after.st_nlink) == (
        before.st_dev,
        before.st_ino,
        before.st_nlink,
    )
    assert sha256_file(legacy) == before_sha256
    assert release_manager._PROCESS_CROSS_LEGACY_LOCK_NAME.fullmatch(
        legacy.name
    )
    assert sorted(path.name for path in runtime.glob("*.lock")) == [
        legacy.name,
        release_manager._PROCESS_CROSS_REGISTRY_LOCK_NAME,
    ]


def test_cross_process_registry_lock_is_released_before_different_key_dispatch(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "different-key-concurrency"
    barrier = threading.Barrier(2)
    entered: list[str] = []
    entered_lock = threading.Lock()

    def dispatch(label: str) -> release_manager._ProcessObserverTransportResult:
        with entered_lock:
            entered.append(label)
        # The falsifier is whether both distinct-key dispatches can enter before
        # either one returns, not whether a saturated filesystem schedules both
        # worker threads within two seconds. A retained registry lock still
        # forces this bounded barrier to fail.
        barrier.wait(timeout=_TEST_CONCURRENCY_BARRIER_TIMEOUT_SECONDS)
        return release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            label.encode("ascii") + b"\n",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            release_manager._process_observer_cross_process_singleflight,
            ("different-key", "one"),
            expected_uid=uid,
            runtime_root=runtime,
            dispatch=lambda: dispatch("one"),
        )
        second = executor.submit(
            release_manager._process_observer_cross_process_singleflight,
            ("different-key", "two"),
            expected_uid=uid,
            runtime_root=runtime,
            dispatch=lambda: dispatch("two"),
        )
        results = (first.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS), second.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS))

    assert sorted(entered) == ["one", "two"]
    assert all(result.cross_process_role == "leader" for result in results)
    assert sorted(path.name for path in runtime.glob("*.lock")) == [
        release_manager._PROCESS_CROSS_REGISTRY_LOCK_NAME
    ]


def test_cross_process_registry_lock_wait_is_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "held-registry-lock",
    )
    lock_path = runtime / release_manager._PROCESS_CROSS_REGISTRY_LOCK_NAME
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        release_manager.fcntl.flock(descriptor, release_manager.fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(ReleaseError, match="timed out"):
            with release_manager._cross_process_lock(
                runtime,
                expected_uid=uid,
                wait_seconds=0.03,
            ):
                raise AssertionError("unreachable")
        elapsed = time.monotonic() - started
        assert 0.02 <= elapsed < 0.5
    finally:
        release_manager.fcntl.flock(descriptor, release_manager.fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.parametrize("shape", ["symlink", "directory", "wrong-mode", "hard-link"])
def test_cross_process_registry_lock_rejects_unsafe_shapes(
    tmp_path: Path,
    shape: str,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / shape,
    )
    lock_path = runtime / release_manager._PROCESS_CROSS_REGISTRY_LOCK_NAME
    if shape == "symlink":
        target = runtime / "target"
        target.write_bytes(b"")
        target.chmod(0o600)
        lock_path.symlink_to(target)
    elif shape == "directory":
        lock_path.mkdir(mode=0o700)
    elif shape == "wrong-mode":
        lock_path.write_bytes(b"")
        lock_path.chmod(0o644)
    elif shape == "hard-link":
        target = runtime / "target"
        target.write_bytes(b"")
        target.chmod(0o600)
        os.link(target, lock_path)
    else:  # pragma: no cover - parametrization contract
        raise AssertionError(shape)

    with pytest.raises(ReleaseError, match="registry lock"):
        release_manager._admit_cross_process_flight(
            ("unsafe-registry-lock", shape),
            expected_uid=uid,
            runtime_root=runtime,
        )


def test_cross_process_runtime_and_protocol_files_reject_unsafe_shapes(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    unsafe = tmp_path / "unsafe-runtime"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    with pytest.raises(ReleaseError, match="unsafe"):
        release_manager._canonical_process_cross_root(
            uid,
            runtime_root=unsafe,
        )
    target = tmp_path / "runtime-target"
    target.mkdir(mode=0o700)
    linked_runtime = tmp_path / "linked-runtime"
    linked_runtime.symlink_to(target, target_is_directory=True)
    with pytest.raises(ReleaseError, match="unsafe"):
        release_manager._canonical_process_cross_root(
            uid,
            runtime_root=linked_runtime,
        )
    with pytest.raises(ReleaseError, match="UID"):
        release_manager._canonical_process_cross_root(
            uid + 1,
            runtime_root=tmp_path / "wrong-user-runtime",
        )

    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    wrong_mode = root / "wrong-mode.json"
    wrong_mode.write_text("{}\n", encoding="utf-8")
    wrong_mode.chmod(0o644)
    with pytest.raises(ReleaseError, match="unsafe|oversized"):
        release_manager._read_cross_file(
            wrong_mode,
            expected_uid=uid,
            max_bytes=1024,
        )

    regular = root / "regular.json"
    regular.write_text("{}\n", encoding="utf-8")
    regular.chmod(0o600)
    hardlink = root / "hardlink.json"
    os.link(regular, hardlink)
    with pytest.raises(ReleaseError, match="unsafe|oversized"):
        release_manager._read_cross_file(
            hardlink,
            expected_uid=uid,
            max_bytes=1024,
        )

    symlink = root / "symlink.json"
    symlink.symlink_to(regular)
    with pytest.raises(ReleaseError, match="unsafe|oversized"):
        release_manager._read_cross_file(
            symlink,
            expected_uid=uid,
            max_bytes=1024,
        )

    oversized = root / "oversized.json"
    oversized.write_bytes(b"x" * 1025)
    oversized.chmod(0o600)
    with pytest.raises(ReleaseError, match="unsafe|oversized"):
        release_manager._read_cross_file(
            oversized,
            expected_uid=uid,
            max_bytes=1024,
        )

    noncanonical = root / "noncanonical.json"
    noncanonical.write_text('{"value": 1}\n', encoding="utf-8")
    noncanonical.chmod(0o600)
    with pytest.raises(ReleaseError, match="not canonical"):
        release_manager._read_cross_json(
            noncanonical,
            expected_uid=uid,
            max_bytes=1024,
        )


@pytest.mark.parametrize(
    "mutation",
    ["pid-reuse", "expired", "wrong-boot", "invalid-schema", "extra-field"],
)
def test_cross_process_stale_or_invalid_leader_is_replaced(
    tmp_path: Path,
    mutation: str,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / mutation
    key = ("stale-leader", mutation)
    first = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
    )
    state = release_manager._read_cross_json(
        first.state_path,
        expected_uid=uid,
        max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
    )
    assert state is not None
    if mutation == "pid-reuse":
        state["leader_start_ticks"] = int(state["leader_start_ticks"]) + 1
    elif mutation == "expired":
        state["started_monotonic_ns"] = (
            time.monotonic_ns() - release_manager._PROCESS_CROSS_MAX_AGE_NS - 1
        )
    elif mutation == "wrong-boot":
        state["boot_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    elif mutation == "invalid-schema":
        state["schema"] = "wrong-schema"
    elif mutation == "extra-field":
        state["unexpected"] = True
    else:  # pragma: no cover - parametrization contract
        raise AssertionError(mutation)
    release_manager._atomic_write_bytes(
        first.state_path,
        canonical_json_bytes(state),
        mode=0o600,
    )

    replacement = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
    )
    assert replacement.role == "leader"
    assert replacement.nonce != first.nonce
    release_manager._clear_matching_cross_state(replacement, expected_uid=uid)


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "key",
        "nonce",
        "result-code",
        "base64",
        "size",
        "digest",
        "publisher-pid",
        "publisher-start",
        "boot",
        "expired",
        "future",
        "extra-field",
    ],
)
def test_cross_process_result_receipt_rejects_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / mutation
    admission = release_manager._admit_cross_process_flight(
        ("result-tamper", mutation),
        expected_uid=uid,
        runtime_root=runtime,
    )
    payload = release_manager._cross_result_payload(
        admission,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"safe-output\n",
        ),
    )
    if mutation == "schema":
        payload["schema"] = "wrong-schema"
    elif mutation == "key":
        payload["key_sha256"] = "0" * 64
    elif mutation == "nonce":
        payload["nonce"] = "0" * 32
    elif mutation == "result-code":
        payload["result_code"] = "NOT VALID"
    elif mutation == "base64":
        payload["stdout_base64"] = "%%%"
    elif mutation == "size":
        payload["stdout_size"] = int(payload["stdout_size"]) + 1
    elif mutation == "digest":
        payload["stdout_sha256"] = "0" * 64
    elif mutation == "publisher-pid":
        payload["publisher_pid"] = admission.leader_pid + 1
    elif mutation == "publisher-start":
        payload["publisher_start_ticks"] = admission.leader_start_ticks + 1
    elif mutation == "boot":
        payload["boot_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    elif mutation == "expired":
        payload["published_monotonic_ns"] = (
            time.monotonic_ns() - release_manager._PROCESS_CROSS_RESULT_TTL_NS - 1
        )
    elif mutation == "future":
        payload["published_monotonic_ns"] = time.monotonic_ns() + 1_000_000_000
    elif mutation == "extra-field":
        payload["unexpected"] = True
    else:  # pragma: no cover - parametrization contract
        raise AssertionError(mutation)
    release_manager._atomic_write_bytes(
        admission.result_path,
        canonical_json_bytes(payload),
        mode=0o600,
    )
    with pytest.raises(ReleaseError, match="invalid"):
        release_manager._read_cross_result(admission, expected_uid=uid)
    release_manager._clear_matching_cross_state(admission, expected_uid=uid)


def test_cross_process_follower_timeout_does_not_dispatch_duplicate(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "timeout"
    key = ("cross-timeout",)
    leader = release_manager._admit_cross_process_flight(
        key,
        expected_uid=uid,
        runtime_root=runtime,
    )
    dispatch = mock.Mock(
        side_effect=AssertionError("cross-process follower dispatched unexpectedly")
    )
    try:
        with mock.patch.object(
            release_manager,
            "_PROCESS_CROSS_WAIT_SECONDS",
            0.01,
        ):
            observed = release_manager._process_observer_cross_process_singleflight(
                key,
                expected_uid=uid,
                dispatch=dispatch,
                runtime_root=runtime,
            )
        dispatch.assert_not_called()
        assert observed.result_code == "cross-process-follower-timeout"
        assert observed.cross_process_role == "follower"
        assert observed.cross_process_shared is True
        assert observed.cross_process_result == "cross-process-follower-timeout"
    finally:
        release_manager._clear_matching_cross_state(leader, expected_uid=uid)


def test_cross_process_unavailable_runtime_falls_back_to_local_dispatch(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    local = release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        b"local\n",
    )
    dispatch = mock.Mock(return_value=local)
    observed = release_manager._process_observer_cross_process_singleflight(
        ("fallback-local",),
        expected_uid=uid,
        dispatch=dispatch,
        runtime_root=unsafe,
    )
    dispatch.assert_called_once_with()
    assert observed == release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        b"local\n",
        "unavailable-local",
        False,
        "unavailable-local",
    )


def test_cross_process_leader_exception_publishes_and_wakes_followers(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "leader-exception"
    key = ("cross-leader-exception",)
    entered = threading.Event()
    release = threading.Event()

    def leader_dispatch() -> release_manager._ProcessObserverTransportResult:
        entered.set()
        assert release.wait(_TEST_CONCURRENCY_RELEASE_TIMEOUT_SECONDS)
        raise RuntimeError("unexpected broker failure")

    follower_dispatch = mock.Mock(
        side_effect=AssertionError("cross-process follower dispatched unexpectedly")
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            release_manager._process_observer_cross_process_singleflight,
            key,
            expected_uid=uid,
            dispatch=leader_dispatch,
            runtime_root=runtime,
        )
        assert entered.wait(_TEST_CONCURRENCY_START_TIMEOUT_SECONDS)
        follower = executor.submit(
            release_manager._process_observer_cross_process_singleflight,
            key,
            expected_uid=uid,
            dispatch=follower_dispatch,
            runtime_root=runtime,
        )
        deadline = time.monotonic() + _TEST_CONCURRENCY_START_TIMEOUT_SECONDS
        while not follower.running():
            assert time.monotonic() < deadline
            time.sleep(0.001)
        time.sleep(_TEST_CONCURRENCY_SETTLE_SECONDS)
        release.set()
        with pytest.raises(RuntimeError, match="unexpected broker failure"):
            leader.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS)
        follower_result = follower.result(timeout=_TEST_CONCURRENCY_RESULT_TIMEOUT_SECONDS)
    follower_dispatch.assert_not_called()
    assert follower_result.result_code == "cross-process-leader-exception"
    assert follower_result.cross_process_role == "follower"
    assert follower_result.cross_process_shared is True
    assert follower_result.cross_process_result == "shared"
    state_files = list(runtime.glob("flight-*.json"))
    assert len(state_files) == 1
    state = release_manager._read_cross_json(
        state_files[0],
        expected_uid=uid,
        max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
    )
    assert state is not None
    assert state["phase"] == "published"
    assert int(state["published_monotonic_ns"]) > 0


def test_cross_process_result_cleanup_and_capacity_are_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "cleanup"
    first = release_manager._admit_cross_process_flight(
        ("cleanup-stale",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    payload = release_manager._cross_result_payload(
        first,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"stale\n",
        ),
    )
    payload["published_monotonic_ns"] = (
        time.monotonic_ns() - release_manager._PROCESS_CROSS_RESULT_TTL_NS - 1
    )
    release_manager._atomic_write_bytes(
        first.result_path,
        canonical_json_bytes(payload),
        mode=0o600,
    )
    release_manager._clear_matching_cross_state(first, expected_uid=uid)
    release_manager._cleanup_cross_results(
        runtime,
        expected_uid=uid,
        current_boot_id=first.boot_id,
    )
    assert not first.result_path.exists()

    fresh = release_manager._admit_cross_process_flight(
        ("cleanup-capacity",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    release_manager._publish_cross_result(
        fresh,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"fresh\n",
        ),
        expected_uid=uid,
    )
    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_MAX_CLEANUP_ENTRIES",
        1,
    ), pytest.raises(ReleaseError, match="capacity"):
        release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=fresh.boot_id,
        )



def test_cross_process_runtime_directory_entry_work_is_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "directory-entry-bound",
    )
    for index in range(5):
        unrelated = runtime / f"unrelated-{index}"
        unrelated.write_bytes(b"inert\n")
        unrelated.chmod(0o600)

    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_MAX_DIRECTORY_ENTRIES",
        4,
        create=True,
    ), pytest.raises(ReleaseError, match="entry bound"):
        release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=release_manager._read_boot_id(Path("/proc")),
        )



def test_cross_process_oversized_state_cleanup_never_widens_to_result_bound(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-state-cleanup-bound",
    )
    state_path = runtime / f"flight-{'a' * 64}.json"
    state_path.write_bytes(b"x" * (1024 * 1024))
    state_path.chmod(0o600)
    original = release_manager._read_cross_file
    calls: list[tuple[int, str, int]] = []

    def traced(path, *, expected_uid, max_bytes):
        try:
            content = original(
                path,
                expected_uid=expected_uid,
                max_bytes=max_bytes,
            )
        except Exception as exc:
            calls.append((max_bytes, type(exc).__name__, 0))
            raise
        calls.append((max_bytes, "returned", 0 if content is None else len(content)))
        return content

    with mock.patch.object(
        release_manager,
        "_read_cross_file",
        side_effect=traced,
    ):
        usage = release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=release_manager._read_boot_id(Path("/proc")),
        )

    assert calls == [
        (
            release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
            "ReleaseError",
            0,
        )
    ]
    assert not state_path.exists()
    assert usage.state_count == 0


def test_cross_process_typed_unlink_removes_oversized_files_without_body_reads(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-oversized-removal",
    )
    state_path = runtime / f"flight-{'b' * 64}.json"
    result_path = runtime / f"result-{'c' * 64}-{'d' * 32}.json"
    state_path.write_bytes(b"s" * (release_manager._PROCESS_CROSS_MAX_STATE_BYTES + 1))
    result_path.write_bytes(b"r" * (release_manager._PROCESS_CROSS_MAX_RESULT_BYTES + 1))
    state_path.chmod(0o600)
    result_path.chmod(0o600)

    with mock.patch.object(
        release_manager.os,
        "read",
        side_effect=AssertionError("oversized removal must not read file bodies"),
    ):
        release_manager._unlink_cross_file(
            state_path,
            expected_uid=uid,
            kind=release_manager._PROCESS_CROSS_FILE_STATE,
        )
        release_manager._unlink_cross_file(
            result_path,
            expected_uid=uid,
            kind=release_manager._PROCESS_CROSS_FILE_RESULT,
        )
    assert not state_path.exists()
    assert not result_path.exists()


def test_cross_process_typed_unlink_reads_small_file_only_within_kind_bound(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-small-removal",
    )
    state_path = runtime / f"flight-{'e' * 64}.json"
    payload = b"small-state-payload"
    state_path.write_bytes(payload)
    state_path.chmod(0o600)
    original_read = os.read
    read_sizes: list[int] = []

    def bounded_read(descriptor, size):
        read_sizes.append(size)
        return original_read(descriptor, size)

    with mock.patch.object(release_manager.os, "read", side_effect=bounded_read):
        release_manager._unlink_cross_file(
            state_path,
            expected_uid=uid,
            kind=release_manager._PROCESS_CROSS_FILE_STATE,
        )
    assert not state_path.exists()
    assert sum(read_sizes) <= len(payload)
    assert max(read_sizes) <= release_manager._PROCESS_CROSS_MAX_STATE_BYTES


def test_cross_process_typed_unlink_rejects_kind_name_mismatch(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-name-mismatch",
    )
    state_path = runtime / f"flight-{'f' * 64}.json"
    state_path.write_bytes(b"{}\n")
    state_path.chmod(0o600)

    with pytest.raises(ReleaseError, match="kind does not match"):
        release_manager._unlink_cross_file(
            state_path,
            expected_uid=uid,
            kind=release_manager._PROCESS_CROSS_FILE_RESULT,
        )
    with pytest.raises(ReleaseError, match="kind is invalid"):
        release_manager._unlink_cross_file(
            state_path,
            expected_uid=uid,
            kind="largest-bound",
        )
    assert state_path.exists()


def test_cross_process_typed_unlink_refuses_unsafe_file_shapes(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-unsafe-shapes",
    )
    target = runtime / "target"
    target.write_bytes(b"target")
    target.chmod(0o600)

    symlink_path = runtime / f"flight-{'1' * 64}.json"
    symlink_path.symlink_to(target.name)
    hardlink_path = runtime / f"flight-{'2' * 64}.json"
    os.link(target, hardlink_path)
    wrong_mode_path = runtime / f"flight-{'3' * 64}.json"
    wrong_mode_path.write_bytes(b"{}")
    wrong_mode_path.chmod(0o644)
    directory_path = runtime / f"flight-{'4' * 64}.json"
    directory_path.mkdir(mode=0o700)

    for candidate in (symlink_path, hardlink_path, wrong_mode_path, directory_path):
        with pytest.raises(ReleaseError, match="unsafe for removal"):
            release_manager._unlink_cross_file(
                candidate,
                expected_uid=uid,
                kind=release_manager._PROCESS_CROSS_FILE_STATE,
            )
        assert os.path.lexists(candidate)
    assert target.read_bytes() == b"target"


def test_cross_process_typed_unlink_refuses_path_identity_replacement(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = release_manager._canonical_process_cross_root(
        uid,
        runtime_root=tmp_path / "typed-identity-replacement",
    )
    state_path = runtime / f"flight-{'5' * 64}.json"
    backup = runtime / "original.backup"
    state_path.write_bytes(b"original")
    state_path.chmod(0o600)
    original_lstat = Path.lstat
    matching_calls = 0

    def racing_lstat(candidate: Path):
        nonlocal matching_calls
        if candidate == state_path:
            matching_calls += 1
            if matching_calls == 2:
                state_path.rename(backup)
                state_path.write_bytes(b"replaced")
                state_path.chmod(0o600)
        return original_lstat(candidate)

    with mock.patch.object(
        Path,
        "lstat",
        autospec=True,
        side_effect=racing_lstat,
    ), pytest.raises(ReleaseError, match="pathname changed"):
        release_manager._unlink_cross_file(
            state_path,
            expected_uid=uid,
            kind=release_manager._PROCESS_CROSS_FILE_STATE,
        )
    assert state_path.read_bytes() == b"replaced"
    assert backup.read_bytes() == b"original"


def test_cross_process_cleanup_uses_one_bounded_directory_snapshot(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "one-directory-snapshot"
    leader = release_manager._admit_cross_process_flight(
        ("one-directory-snapshot",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    release_manager._publish_cross_result(
        leader,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"snapshot\n",
        ),
        expected_uid=uid,
    )
    original_iterdir = Path.iterdir
    with mock.patch.object(
        Path,
        "iterdir",
        autospec=True,
        side_effect=original_iterdir,
    ) as iterdir:
        usage = release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=leader.boot_id,
        )
    assert iterdir.call_count == 1
    assert usage.result_count == 1
    assert usage.state_count == 1
    assert usage.total_result_bytes == leader.result_path.stat().st_size
    assert usage.directory_entries == len(list(runtime.iterdir()))


def test_cross_process_publication_enforces_aggregate_result_bytes_at_commit(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "publication-result-byte-bound"
    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES",
        1,
    ):
        first = release_manager._admit_cross_process_flight(
            ("publication-result-byte-bound", "first"),
            expected_uid=uid,
            runtime_root=runtime,
        )
        second = release_manager._admit_cross_process_flight(
            ("publication-result-byte-bound", "second"),
            expected_uid=uid,
            runtime_root=runtime,
        )
        assert first.role == second.role == "leader"
        for admission in (first, second):
            with pytest.raises(ReleaseError, match="capacity"):
                release_manager._publish_cross_result(
                    admission,
                    release_manager._ProcessObserverTransportResult(
                        "broker-output-ready",
                        b"x",
                    ),
                    expected_uid=uid,
                )
    assert list(runtime.glob("result-*.json")) == []
    for admission in (first, second):
        state = release_manager._read_cross_json(
            admission.state_path,
            expected_uid=uid,
            max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
        )
        assert state is not None
        assert state["phase"] == "running"
        assert state["nonce"] == admission.nonce


def test_cross_process_publication_stages_state_before_final_result_visibility(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "publication-order"
    leader = release_manager._admit_cross_process_flight(
        ("publication-order",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    original_write = release_manager._atomic_write_bytes
    observed: list[str] = []

    def ordered_write(path, content, *, mode=0o644):
        if path == leader.result_path:
            assert not leader.result_path.exists()
            state = release_manager._read_cross_json(
                leader.state_path,
                expected_uid=uid,
                max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
            )
            assert state is not None
            assert state["phase"] == "published"
            assert int(state["published_monotonic_ns"]) > 0
            observed.append("state-before-result")
        return original_write(path, content, mode=mode)

    with mock.patch.object(
        release_manager,
        "_atomic_write_bytes",
        side_effect=ordered_write,
    ):
        published = release_manager._publish_cross_result(
            leader,
            release_manager._ProcessObserverTransportResult(
                "broker-output-ready",
                b"ordered\n",
            ),
            expected_uid=uid,
        )
    assert observed == ["state-before-result"]
    assert published.result_code == "broker-output-ready"
    assert leader.result_path.exists()


def test_cross_process_publication_failure_restores_running_state_and_no_result(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "publication-rollback"
    leader = release_manager._admit_cross_process_flight(
        ("publication-rollback",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    original_write = release_manager._atomic_write_bytes

    def fail_final_result(path, content, *, mode=0o644):
        if path == leader.result_path:
            raise OSError("simulated final result failure")
        return original_write(path, content, mode=mode)

    with mock.patch.object(
        release_manager,
        "_atomic_write_bytes",
        side_effect=fail_final_result,
    ), pytest.raises(ReleaseError, match="rolled back"):
        release_manager._publish_cross_result(
            leader,
            release_manager._ProcessObserverTransportResult(
                "broker-output-ready",
                b"rollback\n",
            ),
            expected_uid=uid,
        )
    assert not leader.result_path.exists()
    state = release_manager._read_cross_json(
        leader.state_path,
        expected_uid=uid,
        max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
    )
    assert state is not None
    assert state["phase"] == "running"
    assert state["published_monotonic_ns"] == 0
    assert state["nonce"] == leader.nonce


def test_cross_process_publication_rejects_exact_aggregate_byte_boundary(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "publication-exact-byte-boundary"
    leader = release_manager._admit_cross_process_flight(
        ("publication-exact-byte-boundary",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    result = release_manager._ProcessObserverTransportResult(
        "broker-output-ready",
        b"boundary\n",
    )
    candidate_size = len(
        canonical_json_bytes(release_manager._cross_result_payload(leader, result))
    )
    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES",
        candidate_size,
    ), pytest.raises(ReleaseError, match="capacity"):
        release_manager._publish_cross_result(
            leader,
            result,
            expected_uid=uid,
        )
    assert not leader.result_path.exists()
    state = release_manager._read_cross_json(
        leader.state_path,
        expected_uid=uid,
        max_bytes=release_manager._PROCESS_CROSS_MAX_STATE_BYTES,
    )
    assert state is not None and state["phase"] == "running"


def test_cross_process_total_result_bytes_are_bounded(
    tmp_path: Path,
) -> None:
    uid = os.getuid()
    runtime = tmp_path / "total-result-byte-bound"
    leader = release_manager._admit_cross_process_flight(
        ("total-result-byte-bound",),
        expected_uid=uid,
        runtime_root=runtime,
    )
    release_manager._publish_cross_result(
        leader,
        release_manager._ProcessObserverTransportResult(
            "broker-output-ready",
            b"bounded-result\n",
        ),
        expected_uid=uid,
    )
    assert leader.result_path.stat().st_size > 1

    with mock.patch.object(
        release_manager,
        "_PROCESS_CROSS_MAX_TOTAL_RESULT_BYTES",
        1,
        create=True,
    ), pytest.raises(ReleaseError, match="capacity"):
        release_manager._cleanup_cross_results(
            runtime,
            expected_uid=uid,
            current_boot_id=leader.boot_id,
        )


def test_trusted_systemd_run_accepts_canonical_system_path_and_rejects_local_copy(
    tmp_path: Path,
) -> None:
    if shutil.which("systemd-run") is None:
        pytest.skip("systemd-run is unavailable")
    trusted = release_manager._trusted_systemd_run()
    assert trusted.is_absolute()
    assert trusted.name == "systemd-run"

    local = tmp_path / "systemd-run"
    local.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    local.chmod(0o755)
    with mock.patch("termdeck.release_manager.shutil.which", return_value=str(local)):
        with pytest.raises(ReleaseError, match="trusted system executable"):
            release_manager._trusted_systemd_run()


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
