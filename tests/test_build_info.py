from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

from termdeck import __version__
from termdeck.build_info import (
    BUILD_INFO_SCHEMA,
    MANAGED_STATE_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    BuildIdentityError,
    canonical_json_bytes,
    compute_build_id,
    compute_release_id,
    load_bundled_build_info,
    release_status,
    sha256_bytes,
    sha256_file,
    validate_build_info,
    validate_release_manifest,
)


def release_build_info(seed: str = "a", *, mode: str = "release") -> dict[str, object]:
    info: dict[str, object] = {
        "schema": BUILD_INFO_SCHEMA,
        "version": "0.2.10",
        "source_commit": seed * 40 + ("+dirty" if mode == "dirty-candidate" else ""),
        "source_tree_sha256": seed * 64,
        "source_date_epoch": 1_700_000_000,
        "build_mode": mode,
    }
    info["build_id"] = compute_build_id(info)
    return info


def release_manifest(
    info: dict[str, object],
    *,
    wheel: str = "t1000-0.2.10-py3-none-any.whl",
    wheel_bytes: bytes = b"wheel-bytes",
) -> dict[str, object]:
    wheel_hash = sha256_bytes(wheel_bytes)
    return {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "release_id": compute_release_id(str(info["build_id"]), wheel_hash),
        "build_info": info,
        "wheel": wheel,
        "wheel_sha256": wheel_hash,
        "wheel_size": len(wheel_bytes),
    }


def activation_identity(tmp_path: Path) -> dict[str, str]:
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_bytes(b"bootstrap\n")
    launcher = tmp_path / "bin" / "t1000"
    launcher.parent.mkdir()
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "bootstrap_sha256": sha256_file(bootstrap),
        "launcher_path": str(launcher),
        "launcher_sha256": sha256_file(launcher),
    }


def test_canonical_json_is_sorted_compact_utf8_and_newline_terminated() -> None:
    assert canonical_json_bytes({"z": "é", "a": [2, 1]}) == (
        '{"a":[2,1],"z":"é"}\n'.encode("utf-8")
    )


def test_development_build_info_is_explicit_and_canonical(tmp_path: Path) -> None:
    source = tmp_path / "_build_info.json"
    source.write_bytes(
        canonical_json_bytes(
            {
                "schema": BUILD_INFO_SCHEMA,
                "version": "0.2.10",
                "source_commit": "development",
                "source_tree_sha256": "",
                "source_date_epoch": 0,
                "build_mode": "development",
                "build_id": "development",
            }
        )
    )
    info = load_bundled_build_info(source)
    assert info["build_mode"] == "development"
    assert info["build_id"] == "development"


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"schema": "other"}, "schema"),
        ({"build_mode": "unknown"}, "build mode"),
        ({"source_date_epoch": -1}, "source_date_epoch"),
        ({"source_tree_sha256": "0" * 63}, "source_tree_sha256"),
        ({"build_id": "0" * 64}, "build_id"),
    ],
)
def test_release_build_info_rejects_schema_domain_and_digest_mismatches(
    mutation: dict[str, object], message: str
) -> None:
    info = release_build_info()
    info.update(mutation)
    with pytest.raises(BuildIdentityError, match=message):
        validate_build_info(info)


def test_build_info_rejects_extra_or_missing_fields() -> None:
    extra = release_build_info()
    extra["host"] = "machine"
    with pytest.raises(BuildIdentityError, match="extras"):
        validate_build_info(extra)

    missing = release_build_info()
    missing.pop("version")
    with pytest.raises(BuildIdentityError, match="version|missing"):
        validate_build_info(missing)


def test_release_manifest_binds_build_and_final_wheel() -> None:
    info = release_build_info()
    manifest = release_manifest(info)
    assert validate_release_manifest(manifest) == manifest

    for field, value in (
        ("wheel_sha256", "0" * 64),
        ("release_id", "0" * 64),
        ("wheel", "../escape.whl"),
        ("wheel_size", 0),
    ):
        candidate = dict(manifest)
        candidate[field] = value
        with pytest.raises(BuildIdentityError):
            validate_release_manifest(candidate)


def test_release_manifest_cannot_claim_development_build() -> None:
    development = {
        "schema": BUILD_INFO_SCHEMA,
        "version": "0.2.10",
        "source_commit": "development",
        "source_tree_sha256": "",
        "source_date_epoch": 0,
        "build_mode": "development",
        "build_id": "development",
    }
    with pytest.raises(BuildIdentityError, match="development"):
        validate_release_manifest(
            {
                "schema": RELEASE_MANIFEST_SCHEMA,
                "release_id": "0" * 64,
                "build_info": development,
                "wheel": "t1000.whl",
                "wheel_sha256": "0" * 64,
                "wheel_size": 1,
            }
        )


def test_release_status_reports_development_without_inventing_release() -> None:
    status = release_status()
    assert status["ok"] is True
    assert status["posture"] == "development"
    assert status["release_id"] is None
    assert status["build_info"]["build_id"] == "development"


def test_repository_release_metadata_matches_package_version() -> None:
    repository = Path(__file__).resolve().parents[1]
    project_document = (repository / "pyproject.toml").read_text(encoding="utf-8")
    project_block = project_document.split("[project]", 1)[1].split("\n[", 1)[0]
    version_match = re.search(
        r'^version\s*=\s*"([^"]+)"\s*$', project_block, re.MULTILINE
    )
    assert version_match is not None
    project_version = version_match.group(1)
    build_info = load_bundled_build_info(repository / "termdeck" / "_build_info.json")

    assert project_version == __version__ == build_info["version"]
    assert f"\n## {project_version}\n" in (repository / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    assert f"\n## {project_version}\n" in (repository / "RELEASE_NOTES.md").read_text(
        encoding="utf-8"
    )
    assert (repository / "docs" / "releases" / f"v{project_version}.md").read_text(
        encoding="utf-8"
    ).startswith(f"# T1000 {project_version}\n")


def test_release_status_rejects_dirty_candidate_as_managed_release(
    tmp_path: Path,
) -> None:
    info = release_build_info("d", mode="dirty-candidate")
    manifest_path = tmp_path / "candidate.release.json"
    manifest_path.write_bytes(canonical_json_bytes(release_manifest(info)))

    with mock.patch(
        "termdeck.build_info.load_bundled_build_info", return_value=info
    ), mock.patch(
        "termdeck.build_info._inferred_manifest_path", return_value=manifest_path
    ):
        status = release_status(managed_root=tmp_path)

    assert status["ok"] is False
    assert status["posture"] == "dirty-candidate"
    assert status["release_id"] is None
    assert any("production release" in error for error in status["errors"])


def test_release_status_verifies_managed_manifest_and_artifact(tmp_path: Path) -> None:
    info = release_build_info("b")
    wheel_bytes = b"verified-wheel"
    manifest = validate_release_manifest(release_manifest(info, wheel_bytes=wheel_bytes))
    release_dir = tmp_path / "releases" / str(manifest["release_id"])
    artifacts = release_dir / "artifacts"
    artifacts.mkdir(parents=True)
    wheel_path = artifacts / str(manifest["wheel"])
    wheel_path.write_bytes(wheel_bytes)
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    (tmp_path / "state.json").write_bytes(
        canonical_json_bytes(
            {
                "schema": MANAGED_STATE_SCHEMA,
                "generation": 1,
                "current_release_id": manifest["release_id"],
                "previous_release_id": None,
                "manifest_sha256": sha256_file(manifest_path),
                **activation_identity(tmp_path),
                "legacy_launcher": None,
            }
        )
    )

    with mock.patch("termdeck.build_info.load_bundled_build_info", return_value=info), mock.patch(
        "termdeck.build_info._inferred_manifest_path", return_value=manifest_path
    ):
        status = release_status(verify_artifact=True, managed_root=tmp_path)

    assert status["ok"] is True
    assert status["posture"] == "managed-release"
    assert status["release_id"] == manifest["release_id"]
    assert status["artifact_verified"] is True


def test_release_status_detects_manifest_state_and_artifact_tamper(tmp_path: Path) -> None:
    info = release_build_info("c")
    wheel_bytes = b"wheel"
    manifest = validate_release_manifest(release_manifest(info, wheel_bytes=wheel_bytes))
    release_dir = tmp_path / "releases" / str(manifest["release_id"])
    artifacts = release_dir / "artifacts"
    artifacts.mkdir(parents=True)
    wheel_path = artifacts / str(manifest["wheel"])
    wheel_path.write_bytes(b"tampered")
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    (tmp_path / "state.json").write_bytes(
        canonical_json_bytes(
            {
                "schema": MANAGED_STATE_SCHEMA,
                "generation": 1,
                "current_release_id": "d" * 64,
                "previous_release_id": None,
                "manifest_sha256": "e" * 64,
                **activation_identity(tmp_path),
                "legacy_launcher": None,
            }
        )
    )

    with mock.patch("termdeck.build_info.load_bundled_build_info", return_value=info), mock.patch(
        "termdeck.build_info._inferred_manifest_path", return_value=manifest_path
    ):
        status = release_status(verify_artifact=True, managed_root=tmp_path)

    assert status["ok"] is False
    assert status["posture"] == "managed-invalid"
    assert any("current_release_id" in error for error in status["errors"])
    assert any("manifest hash" in error for error in status["errors"])
    assert any("wheel" in error for error in status["errors"])


def test_json_reader_rejects_non_utf8_and_non_json(tmp_path: Path) -> None:
    from termdeck.build_info import read_json

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(BuildIdentityError, match="UTF-8 JSON"):
        read_json(invalid_utf8, label="test")

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(BuildIdentityError, match="UTF-8 JSON"):
        read_json(invalid_json, label="test")
