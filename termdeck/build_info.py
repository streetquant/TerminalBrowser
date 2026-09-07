"""Immutable source/build/release identity for T1000.

A wheel cannot contain its own hash.  T1000 therefore uses two levels:

* bundled build info binds version, source commit, source-tree SHA-256, and the
  deterministic source epoch into ``build_id``;
* the external release manifest binds that build to the final wheel SHA-256 and
  derives ``release_id``.

Managed launchers verify both levels before importing the application.  Source
and editable executions deliberately report ``development`` rather than
pretending to be a production release.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

BUILD_INFO_SCHEMA = "t1000.build-info.v1"
RELEASE_MANIFEST_SCHEMA = "t1000.release-manifest.v1"
MANAGED_STATE_SCHEMA = "t1000.managed-state.v2"
_BUILD_INFO_NAME = "_build_info.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")
_BUILD_MODES = frozenset({"development", "release", "dirty-candidate"})
_BUILD_ID_FIELDS = (
    "schema",
    "version",
    "source_commit",
    "source_tree_sha256",
    "source_date_epoch",
    "build_mode",
)


class BuildIdentityError(ValueError):
    """Raised when build or release metadata is malformed or inconsistent."""


def canonical_json_bytes(value: object) -> bytes:
    """Return one stable UTF-8 JSON encoding suitable for content addressing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BuildIdentityError(f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BuildIdentityError(f"{key} must be a non-empty string")
    return value


def build_identity_fields(build_info: Mapping[str, object]) -> dict[str, object]:
    """Select the exact non-self-referential fields covered by ``build_id``."""

    return {field: build_info.get(field) for field in _BUILD_ID_FIELDS}


def compute_build_id(build_info: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(build_identity_fields(build_info)))


def compute_release_id(build_id: str, wheel_sha256: str) -> str:
    if not _SHA256.fullmatch(build_id):
        raise BuildIdentityError("build_id is not a SHA-256 digest")
    if not _SHA256.fullmatch(wheel_sha256):
        raise BuildIdentityError("wheel_sha256 is not a SHA-256 digest")
    return sha256_bytes(
        canonical_json_bytes(
            {"build_id": build_id, "wheel_sha256": wheel_sha256}
        )
    )


def validate_build_info(value: object) -> dict[str, object]:
    info = _plain_mapping(value, label="build info")
    if info.get("schema") != BUILD_INFO_SCHEMA:
        raise BuildIdentityError("unsupported build-info schema")
    version = _required_string(info, "version")
    if any(character.isspace() for character in version):
        raise BuildIdentityError("version contains whitespace")
    source_commit = _required_string(info, "source_commit")
    build_mode = _required_string(info, "build_mode")
    if build_mode not in _BUILD_MODES:
        raise BuildIdentityError(f"unsupported build mode: {build_mode}")
    epoch = info.get("source_date_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise BuildIdentityError("source_date_epoch must be a non-negative integer")
    tree_hash = info.get("source_tree_sha256")
    build_id = info.get("build_id")

    if build_mode == "development":
        if source_commit != "development" or tree_hash != "" or build_id != "development":
            raise BuildIdentityError("development build info is not canonical")
    else:
        if not isinstance(tree_hash, str) or not _SHA256.fullmatch(tree_hash):
            raise BuildIdentityError("source_tree_sha256 is not a SHA-256 digest")
        if not isinstance(build_id, str) or not _SHA256.fullmatch(build_id):
            raise BuildIdentityError("build_id is not a SHA-256 digest")
        if compute_build_id(info) != build_id:
            raise BuildIdentityError("build_id does not match bundled source identity")

    allowed = set(_BUILD_ID_FIELDS) | {"build_id"}
    if set(info) != allowed:
        extras = sorted(set(info) - allowed)
        missing = sorted(allowed - set(info))
        raise BuildIdentityError(
            f"build info fields differ from schema; extras={extras}, missing={missing}"
        )
    return {key: info[key] for key in sorted(info)}


def validate_release_manifest(value: object) -> dict[str, object]:
    manifest = _plain_mapping(value, label="release manifest")
    expected_fields = {
        "schema",
        "release_id",
        "build_info",
        "wheel",
        "wheel_sha256",
        "wheel_size",
    }
    if set(manifest) != expected_fields:
        extras = sorted(set(manifest) - expected_fields)
        missing = sorted(expected_fields - set(manifest))
        raise BuildIdentityError(
            f"manifest fields differ from schema; extras={extras}, missing={missing}"
        )
    if manifest.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise BuildIdentityError("unsupported release-manifest schema")
    build_info = validate_build_info(manifest.get("build_info"))
    if build_info["build_mode"] == "development":
        raise BuildIdentityError("a development build cannot be a release artifact")
    wheel = _required_string(manifest, "wheel")
    if Path(wheel).name != wheel or wheel in {".", ".."}:
        raise BuildIdentityError("wheel must be one safe basename")
    wheel_hash = _required_string(manifest, "wheel_sha256")
    if not _SHA256.fullmatch(wheel_hash):
        raise BuildIdentityError("wheel_sha256 is not a SHA-256 digest")
    wheel_size = manifest.get("wheel_size")
    if isinstance(wheel_size, bool) or not isinstance(wheel_size, int) or wheel_size <= 0:
        raise BuildIdentityError("wheel_size must be a positive integer")
    release_id = _required_string(manifest, "release_id")
    if not _RELEASE_ID.fullmatch(release_id):
        raise BuildIdentityError("release_id is not a SHA-256 digest")
    if compute_release_id(str(build_info["build_id"]), wheel_hash) != release_id:
        raise BuildIdentityError("release_id does not bind build_id and wheel_sha256")
    return {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "release_id": release_id,
        "build_info": build_info,
        "wheel": wheel,
        "wheel_sha256": wheel_hash,
        "wheel_size": wheel_size,
    }


def validate_managed_state(value: object) -> dict[str, object]:
    """Validate the complete activation identity selected by ``state.json``."""

    state = _plain_mapping(value, label="managed state")
    fields = {
        "schema",
        "generation",
        "current_release_id",
        "previous_release_id",
        "manifest_sha256",
        "python_executable",
        "bootstrap_sha256",
        "launcher_path",
        "launcher_sha256",
        "legacy_launcher",
    }
    if set(state) != fields:
        extras = sorted(set(state) - fields)
        missing = sorted(fields - set(state))
        raise BuildIdentityError(
            f"managed state fields differ from schema; extras={extras}, missing={missing}"
        )
    if state.get("schema") != MANAGED_STATE_SCHEMA:
        raise BuildIdentityError("unsupported managed-state schema")
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise BuildIdentityError("managed state generation is invalid")
    for key in ("current_release_id", "previous_release_id"):
        release_id = state.get(key)
        if release_id is not None and (
            not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id)
        ):
            raise BuildIdentityError(f"managed state {key} is invalid")
    current = state.get("current_release_id")
    previous = state.get("previous_release_id")
    if current is not None and current == previous:
        raise BuildIdentityError("managed current and previous releases must differ")

    manifest_hash = state.get("manifest_sha256")
    python_executable = state.get("python_executable")
    bootstrap_hash = state.get("bootstrap_sha256")
    launcher_path = state.get("launcher_path")
    launcher_hash = state.get("launcher_sha256")
    string_fields = {
        "manifest_sha256": manifest_hash,
        "python_executable": python_executable,
        "bootstrap_sha256": bootstrap_hash,
        "launcher_path": launcher_path,
        "launcher_sha256": launcher_hash,
    }
    if any(not isinstance(item, str) for item in string_fields.values()):
        raise BuildIdentityError("managed activation identity fields must be strings")
    assert isinstance(manifest_hash, str)
    assert isinstance(python_executable, str)
    assert isinstance(bootstrap_hash, str)
    assert isinstance(launcher_path, str)
    assert isinstance(launcher_hash, str)

    if current is None:
        if manifest_hash or python_executable or bootstrap_hash:
            raise BuildIdentityError(
                "managed state without a current release must clear release activation fields"
            )
        if bool(launcher_path) != bool(launcher_hash):
            raise BuildIdentityError(
                "legacy launcher path and SHA-256 must either both be set or both be empty"
            )
        if launcher_hash and not _SHA256.fullmatch(launcher_hash):
            raise BuildIdentityError("managed launcher_sha256 is invalid")
    else:
        for key, digest in (
            ("manifest_sha256", manifest_hash),
            ("bootstrap_sha256", bootstrap_hash),
            ("launcher_sha256", launcher_hash),
        ):
            if not _SHA256.fullmatch(digest):
                raise BuildIdentityError(f"managed state {key} is invalid")
        if not python_executable or "\x00" in python_executable or not Path(python_executable).is_absolute():
            raise BuildIdentityError("managed python_executable must be one absolute path")
        if not launcher_path or "\x00" in launcher_path or not Path(launcher_path).is_absolute():
            raise BuildIdentityError("managed launcher_path must be one absolute path")
    if launcher_path and ("\x00" in launcher_path or not Path(launcher_path).is_absolute()):
        raise BuildIdentityError("managed launcher_path must be one absolute path")

    legacy = state.get("legacy_launcher")
    if legacy is not None:
        if not isinstance(legacy, Mapping):
            raise BuildIdentityError("legacy_launcher must be null or an object")
        legacy_fields = {str(key): item for key, item in legacy.items()}
        if set(legacy_fields) != {"path", "sha256", "mode"}:
            raise BuildIdentityError("legacy_launcher fields are invalid")
        legacy_path = legacy_fields.get("path")
        legacy_hash = legacy_fields.get("sha256")
        legacy_mode = legacy_fields.get("mode")
        if (
            not isinstance(legacy_path, str)
            or not legacy_path
            or "\x00" in legacy_path
            or not Path(legacy_path).is_absolute()
        ):
            raise BuildIdentityError("legacy_launcher path is invalid")
        if not isinstance(legacy_hash, str) or not _SHA256.fullmatch(legacy_hash):
            raise BuildIdentityError("legacy_launcher SHA-256 is invalid")
        if (
            isinstance(legacy_mode, bool)
            or not isinstance(legacy_mode, int)
            or legacy_mode < 0
            or legacy_mode > 0o777
            or legacy_mode & 0o100 == 0
            or legacy_mode & 0o022 != 0
        ):
            raise BuildIdentityError(
                "legacy_launcher mode must be owner-executable and not group/world writable"
            )
        state["legacy_launcher"] = {
            "path": legacy_path,
            "sha256": legacy_hash,
            "mode": legacy_mode,
        }
    return {key: state[key] for key in sorted(state)}


def read_json(path: str | Path, *, label: str) -> object:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise BuildIdentityError(f"could not read {label}: {source}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildIdentityError(f"{label} is not valid UTF-8 JSON: {source}") from exc


def bundled_build_info_path() -> Path:
    return Path(__file__).resolve().with_name(_BUILD_INFO_NAME)


def load_bundled_build_info(path: str | Path | None = None) -> dict[str, object]:
    source = Path(path) if path is not None else bundled_build_info_path()
    return validate_build_info(read_json(source, label="bundled build info"))


def default_managed_root(*, env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get("T1000_MANAGED_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    from .storage_fabric import DEFAULT_MANAGED_ROOT

    configured_home = environment.get("HOME", "").strip()
    home = Path(configured_home) if configured_home else Path.home()
    return home / DEFAULT_MANAGED_ROOT


def _inferred_manifest_path() -> Path | None:
    explicit = os.environ.get("T1000_RELEASE_MANIFEST", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    package_dir = Path(__file__).resolve().parent
    candidate = package_dir.parent.parent / "manifest.json"
    return candidate if candidate.is_file() else None


def _read_managed_state(root: Path) -> tuple[dict[str, object] | None, str]:
    path = root / "state.json"
    if not path.exists():
        return None, ""
    try:
        raw = path.read_bytes()
        value = validate_managed_state(read_json(path, label="managed state"))
        if raw != canonical_json_bytes(value):
            raise BuildIdentityError("managed state is not canonically serialized")
    except (OSError, BuildIdentityError) as exc:
        return None, str(exc)
    return value, ""


def _hash_regular_file(path: Path, *, label: str) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BuildIdentityError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise BuildIdentityError(f"{label} is not a regular file: {path}")
    try:
        return sha256_file(path)
    except OSError as exc:
        raise BuildIdentityError(f"could not hash {label}: {path}: {exc}") from exc


def release_status(
    *,
    verify_artifact: bool = False,
    managed_root: str | Path | None = None,
) -> dict[str, object]:
    """Return a redaction-safe identity/integrity report for the running code."""

    result: dict[str, object] = {
        "schema": "t1000.release-status.v1",
        "posture": "invalid-build-info",
        "ok": False,
        "module_path": str(Path(__file__).resolve()),
        "build_info": None,
        "release_id": None,
        "manifest_path": "",
        "artifact_verified": False,
        "errors": [],
    }
    errors: list[str] = result["errors"]  # type: ignore[assignment]
    try:
        build_info = load_bundled_build_info()
    except BuildIdentityError as exc:
        errors.append(str(exc))
        return result
    result["build_info"] = build_info

    root = Path(managed_root).expanduser() if managed_root is not None else default_managed_root()
    state, state_error = _read_managed_state(root)
    if state_error:
        errors.append(state_error)
    result["managed_root"] = str(root)
    result["managed_current_release_id"] = state.get("current_release_id") if state else None

    manifest_path = _inferred_manifest_path()
    build_mode = str(build_info["build_mode"])
    if build_mode != "release":
        if manifest_path is not None:
            result["manifest_path"] = str(manifest_path)
            errors.append(
                f"{build_mode} build cannot be reported as a managed production release"
            )
        result.update({"posture": build_mode, "ok": not errors})
        return result
    if manifest_path is None:
        result.update({"posture": "unmanaged-wheel", "ok": not errors})
        return result

    result["manifest_path"] = str(manifest_path)
    try:
        manifest = validate_release_manifest(
            read_json(manifest_path, label="release manifest")
        )
    except BuildIdentityError as exc:
        errors.append(str(exc))
        result["posture"] = "managed-invalid"
        return result
    if manifest["build_info"] != build_info:
        errors.append("bundled build info does not match the managed manifest")
    release_id = str(manifest["release_id"])
    result["release_id"] = release_id
    if state is None:
        errors.append("running managed release has no valid managed state")
    else:
        if state.get("current_release_id") != release_id:
            errors.append("running release does not match managed current_release_id")
        expected_manifest_hash = str(state.get("manifest_sha256", ""))
        try:
            observed_manifest_hash = _hash_regular_file(
                manifest_path, label="managed manifest"
            )
        except BuildIdentityError as exc:
            errors.append(str(exc))
        else:
            if observed_manifest_hash != expected_manifest_hash:
                errors.append("managed manifest hash does not match state.json")

        bootstrap = root / "bootstrap.py"
        launcher = Path(str(state.get("launcher_path", "")))
        expected_bootstrap_hash = str(state.get("bootstrap_sha256", ""))
        expected_launcher_hash = str(state.get("launcher_sha256", ""))
        for path, label, expected in (
            (bootstrap, "managed bootstrap", expected_bootstrap_hash),
            (launcher, "managed launcher", expected_launcher_hash),
        ):
            try:
                observed = _hash_regular_file(path, label=label)
            except BuildIdentityError as exc:
                errors.append(str(exc))
            else:
                if observed != expected:
                    errors.append(f"{label} SHA-256 does not match state.json")
        configured_python = str(state.get("python_executable", ""))
        try:
            running_python = str(Path(sys.executable).resolve(strict=True))
        except OSError as exc:
            errors.append(f"could not resolve running Python interpreter: {exc}")
        else:
            if configured_python != running_python:
                errors.append("running Python interpreter does not match state.json")

    artifact = manifest_path.parent / "artifacts" / str(manifest["wheel"])
    result["wheel_path"] = str(artifact)
    result["wheel_sha256"] = manifest["wheel_sha256"]
    if verify_artifact:
        try:
            size = artifact.stat().st_size
            digest = sha256_file(artifact)
        except OSError as exc:
            errors.append(f"could not verify managed wheel: {exc}")
        else:
            if size != manifest["wheel_size"]:
                errors.append("managed wheel size does not match manifest")
            if digest != manifest["wheel_sha256"]:
                errors.append("managed wheel SHA-256 does not match manifest")
            result["artifact_verified"] = not errors

    result["posture"] = "managed-release" if not errors else "managed-invalid"
    result["ok"] = not errors
    return result
