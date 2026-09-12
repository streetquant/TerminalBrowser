#!/usr/bin/env python3
"""Run TermDeck's authoritative bounded test and smoke validation contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
import uuid

# Running this file by path places only ``scripts`` on ``sys.path``.  Pin the
# validation runner to the checkout before any production preflight imports
# TermDeck so that its storage and release checks exercise this exact source.
_REPOSITORY = Path(__file__).resolve().parents[1]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

_MANIFEST_SCHEMA = "t1000.validation-manifest.v1"
_RECEIPT_SCHEMA = "t1000.validation-receipt.v1"
_RUNNING_SCHEMA = "t1000.validation-running.v1"
_PROFILES = ("ci", "production")
_ENVIRONMENTS = ("production-host", "xvfb-dbus")
_PRODUCTION_ONLY_SMOKE = "scripts/check_managed_process_inventory_smoke.py"
_TOP_LEVEL_KEYS = {"limits", "profiles", "schema", "smokes", "tests"}
_TEST_KEYS = {"pattern", "profiles", "timeout_seconds"}
_LIMIT_KEYS = {
    "smoke_json_bytes",
    "stderr_bytes",
    "stdout_bytes",
    "termination_grace_seconds",
}
_SMOKE_KEYS = {"environment", "path", "profiles", "timeout_seconds"}
_TEST_SUMMARY_PATTERNS = {
    "passed": re.compile(r"(\d+) passed"),
    "skipped": re.compile(r"(\d+) skipped"),
    "warnings": re.compile(r"(\d+) warnings?"),
    "subtests_passed": re.compile(r"(\d+) subtests? passed"),
    "xfailed": re.compile(r"(\d+) xfailed"),
    "xpassed": re.compile(r"(\d+) xpassed"),
}
_TASK_RUNTIME_BASE = Path("/tmp")
_TASK_RUNTIME_BASE_ENV = "T1000_VALIDATION_RUNTIME_BASE"
_TASK_RUNTIME_POLICY_ENV = "T1000_VALIDATION_RUNTIME_POLICY"
_TASK_RUNTIME_MAX_PATH_BYTES = 64
_TASK_RUNTIME_PORTABLE_NAME = "t1000-validation"
_TASK_RUNTIME_FALLBACK_PREFIX = "t1000-validation-"
_TASK_RUNTIME_PATH_SAMPLE = "t1v-00000000-00000000"
_TASK_RUNTIME_LEASE_NAME = ".t1000-validation-owner.json"
_TASK_RUNTIME_LEASE_SCHEMA = "t1000.validation-runtime-owner.v1"
_TASK_RUNTIME_MAX_LEASE_BYTES = 4096
_TASK_RUNTIME_MAX_SCAN_ENTRIES = 256
_TASK_RUNTIME_MAX_REMOVALS = 32
_TASK_RUNTIME_MAX_REAPER_CURSORS = 16
_TASK_RUNTIME_NAME = re.compile(r"^t1v-[0-9a-f]{8}-[a-z0-9_]{8}$")
_TASK_RUNTIME_QUARANTINE_NAME = re.compile(r"^t1v-q-[0-9a-f]{32}$")
_TASK_RUNTIME_LEASE_KEYS = {
    "boot_id",
    "created_ns",
    "device",
    "inode",
    "pid",
    "schema",
    "start_ticks",
    "uid",
}
_INHERITED_SUBPROCESS_ENV_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "SHELL",
        "SOURCE_DATE_EPOCH",
        "T1000_EXPECTED_SITE",
        "T1000_MANAGED_BIN_DIR",
        "T1000_MANAGED_ROOT",
        "T1000_RELEASE_MANIFEST",
        "T1000_RELEASE_STATE",
        _TASK_RUNTIME_BASE_ENV,
        "TERM",
        "TERMDECK_REAL_CODEX_BINARY",
        "TERMINFO",
        "TERMINFO_DIRS",
        "TMPDIR",
        "TZ",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_DIRS",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)
_VALIDATION_SUBPROCESS_ENV_KEYS = frozenset(
    {
        "NO_AT_BRIDGE",
        "PYTHONBREAKPOINT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "TMUX_TMPDIR",
        _TASK_RUNTIME_POLICY_ENV,
    }
)
_TASK_RUNTIME_REAPER_CURSOR_LOCK = threading.Lock()
_TASK_RUNTIME_REAPER_CURSORS: dict[tuple[str, int, int], Any] = {}


class ValidationError(RuntimeError):
    """Raised when the validation contract cannot be trusted or completed."""


def _subprocess_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return the closed environment contract for every validation child."""

    permitted = _INHERITED_SUBPROCESS_ENV_KEYS | _VALIDATION_SUBPROCESS_ENV_KEYS
    environment = {
        key: str(value)
        for key, value in source.items()
        if key in permitted and isinstance(value, str)
    }
    environment.setdefault("PATH", os.defpath)
    return environment


class _TaskRuntime:
    """Identity and crash-recovery lease for one short external runtime."""

    __slots__ = (
        "path",
        "device",
        "inode",
        "uid",
        "owner_pid",
        "owner_start_ticks",
        "boot_id",
        "lease_path",
    )

    def __init__(
        self,
        *,
        path: Path,
        device: int,
        inode: int,
        uid: int,
        owner_pid: int,
        owner_start_ticks: int,
        boot_id: str,
        lease_path: Path,
    ) -> None:
        self.path = path
        self.device = int(device)
        self.inode = int(inode)
        self.uid = int(uid)
        self.owner_pid = int(owner_pid)
        self.owner_start_ticks = int(owner_start_ticks)
        self.boot_id = str(boot_id)
        self.lease_path = lease_path


def canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(data: bytes | str) -> object:
    try:
        return json.loads(data, object_pairs_hook=_strict_object)
    except ValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        content = canonical_json_bytes(payload)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _validate_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{name} is outside [{minimum}, {maximum}]")
    return value


def _validate_closed_keys(payload: Mapping[str, object], expected: set[str], *, name: str) -> None:
    observed = set(payload)
    if observed != expected:
        raise ValidationError(
            f"{name} keys differ: missing={sorted(expected - observed)}, "
            f"extra={sorted(observed - expected)}"
        )


def _direct_regular(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValidationError(f"validation path is not a safe direct relative path: {relative}")
    path = root.joinpath(candidate)
    try:
        resolved_parent = path.parent.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise ValidationError(f"validation path cannot be resolved safely: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"validation path is not a direct regular file: {relative}")
    return path


def discover_inventory(root: Path) -> tuple[list[str], list[str]]:
    tests = sorted(f"tests/{path.name}" for path in (root / "tests").glob("test_*.py"))
    smokes = sorted(
        f"scripts/{path.name}" for path in (root / "scripts").glob("check_*_smoke.py")
    )
    if not tests:
        raise ValidationError("no test files were discovered")
    if not smokes:
        raise ValidationError("no smoke files were discovered")
    for relative in [*tests, *smokes]:
        _direct_regular(root, relative)
    return tests, smokes


def validate_manifest_payload(
    root: Path,
    payload: object,
    *,
    discovered_tests: Sequence[str] | None = None,
    discovered_smokes: Sequence[str] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValidationError("validation manifest must be a JSON object")
    _validate_closed_keys(payload, _TOP_LEVEL_KEYS, name="manifest")
    if payload.get("schema") != _MANIFEST_SCHEMA:
        raise ValidationError("validation manifest schema is unsupported")
    profiles = payload.get("profiles")
    if profiles != list(_PROFILES):
        raise ValidationError(f"validation profiles must be exactly {list(_PROFILES)}")

    tests = payload.get("tests")
    if not isinstance(tests, dict):
        raise ValidationError("manifest tests must be an object")
    _validate_closed_keys(tests, _TEST_KEYS, name="manifest tests")
    if tests.get("pattern") != "tests/test_*.py":
        raise ValidationError("manifest test pattern must be tests/test_*.py")
    if tests.get("profiles") != list(_PROFILES):
        raise ValidationError("manifest tests must run in ci and production")
    _validate_int(
        tests.get("timeout_seconds"),
        name="tests timeout_seconds",
        minimum=1,
        maximum=3600,
    )

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise ValidationError("manifest limits must be an object")
    _validate_closed_keys(limits, _LIMIT_KEYS, name="manifest limits")
    _validate_int(
        limits.get("stdout_bytes"),
        name="stdout_bytes",
        minimum=4096,
        maximum=64 * 1024 * 1024,
    )
    _validate_int(
        limits.get("stderr_bytes"),
        name="stderr_bytes",
        minimum=4096,
        maximum=64 * 1024 * 1024,
    )
    smoke_json_limit = _validate_int(
        limits.get("smoke_json_bytes"),
        name="smoke_json_bytes",
        minimum=1024,
        maximum=8 * 1024 * 1024,
    )
    if smoke_json_limit > int(limits["stdout_bytes"]):
        raise ValidationError("smoke_json_bytes cannot exceed stdout_bytes")
    _validate_int(
        limits.get("termination_grace_seconds"),
        name="termination_grace_seconds",
        minimum=1,
        maximum=30,
    )

    entries = payload.get("smokes")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("manifest smokes must be a nonempty array")
    paths: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValidationError(f"smoke entry {index} must be an object")
        _validate_closed_keys(entry, _SMOKE_KEYS, name=f"smoke entry {index}")
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise ValidationError(f"smoke entry {index} path must be a string")
        _direct_regular(root, relative)
        if not re.fullmatch(r"scripts/check_[a-z0-9_]+_smoke\.py", relative):
            raise ValidationError(f"smoke entry {index} path has the wrong grammar")
        entry_profiles = entry.get("profiles")
        if not isinstance(entry_profiles, list) or entry_profiles != sorted(set(entry_profiles)):
            raise ValidationError(f"smoke entry {index} profiles must be sorted and unique")
        if any(profile not in _PROFILES for profile in entry_profiles):
            raise ValidationError(f"smoke entry {index} has an unknown profile")
        environment = entry.get("environment")
        if environment not in _ENVIRONMENTS:
            raise ValidationError(f"smoke entry {index} has an unknown environment")
        if relative == _PRODUCTION_ONLY_SMOKE:
            if entry_profiles != ["production"] or environment != "production-host":
                raise ValidationError("managed process inventory must be production-host only")
        elif entry_profiles != list(_PROFILES) or environment != "xvfb-dbus":
            raise ValidationError(
                f"portable smoke {relative} must run in ci and production via xvfb-dbus"
            )
        _validate_int(
            entry.get("timeout_seconds"),
            name=f"smoke entry {index} timeout_seconds",
            minimum=1,
            maximum=3600,
        )
        paths.append(relative)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValidationError("manifest smoke paths must be sorted and unique")

    actual_tests, actual_smokes = (
        (list(discovered_tests), list(discovered_smokes))
        if discovered_tests is not None and discovered_smokes is not None
        else discover_inventory(root)
    )
    if paths != list(actual_smokes):
        raise ValidationError(
            "manifest smoke inventory drift: "
            f"missing={sorted(set(actual_smokes) - set(paths))}, "
            f"extra={sorted(set(paths) - set(actual_smokes))}"
        )
    if not actual_tests or actual_tests != sorted(set(actual_tests)):
        raise ValidationError("discovered test inventory is empty, duplicated, or unsorted")
    return payload


def load_manifest(root: Path, path: Path) -> tuple[dict[str, object], bytes, list[str], list[str]]:
    manifest = _direct_regular(root, str(path.relative_to(root)))
    raw = manifest.read_bytes()
    payload = strict_json_loads(raw)
    if raw != canonical_json_bytes(payload):
        raise ValidationError("validation manifest is not canonical JSON")
    tests, smokes = discover_inventory(root)
    return validate_manifest_payload(
        root,
        payload,
        discovered_tests=tests,
        discovered_smokes=smokes,
    ), raw, tests, smokes


def selected_inventory(
    manifest: Mapping[str, object],
    *,
    profile: str,
    tests_only: bool,
    smokes_only: bool,
    discovered_tests: Sequence[str],
) -> tuple[list[str], list[dict[str, object]]]:
    if profile not in _PROFILES:
        raise ValidationError(f"unknown validation profile: {profile}")
    if tests_only and smokes_only:
        raise ValidationError("tests-only and smokes-only are mutually exclusive")
    selected_tests = [] if smokes_only else list(discovered_tests)
    selected_smokes: list[dict[str, object]] = []
    if not tests_only:
        for entry in manifest["smokes"]:  # type: ignore[index]
            assert isinstance(entry, dict)
            profiles = entry.get("profiles")
            if isinstance(profiles, list) and profile in profiles:
                selected_smokes.append(dict(entry))
    return selected_tests, selected_smokes


def _git_source_paths(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            env=_subprocess_environment(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"source inventory command failed: {exc}") from exc
    if completed.returncode != 0:
        raise ValidationError(
            "source inventory command failed: "
            + completed.stderr.decode("utf-8", "replace")[-1000:]
        )
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    )


def source_fingerprint(root: Path, *, excluded_root: Path | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    excluded_relative: Path | None = None
    if excluded_root is not None:
        try:
            excluded_relative = excluded_root.absolute().relative_to(root)
        except ValueError:
            excluded_relative = None
    entries: list[dict[str, object]] = []
    for relative in _git_source_paths(root):
        relative_path = Path(relative)
        if excluded_relative is not None and (
            relative_path == excluded_relative or excluded_relative in relative_path.parents
        ):
            continue
        candidate = root / relative_path
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ValidationError(f"source path cannot be inspected: {relative}") from exc
        if stat.S_ISREG(metadata.st_mode):
            content_sha256 = sha256_file(candidate)
            kind = "file"
        elif stat.S_ISLNK(metadata.st_mode):
            content_sha256 = sha256_bytes(os.readlink(candidate).encode("utf-8", "surrogateescape"))
            kind = "symlink"
        else:
            raise ValidationError(f"source inventory contains a special file: {relative}")
        entries.append(
            {
                "kind": kind,
                "mode": stat.S_IMODE(metadata.st_mode),
                "path": relative,
                "sha256": content_sha256,
                "size": metadata.st_size,
            }
        )
    encoded = canonical_json_bytes(entries)
    return {"entry_count": len(entries), "sha256": sha256_bytes(encoded)}


def _safe_output_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    if os.path.lexists(path):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("validation output path is not a direct directory")
        if any(path.iterdir()):
            raise ValidationError("validation output directory must be empty")
        path.chmod(0o700)
    else:
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    return path


def _tool_output(argv: Sequence[str], *, cwd: Path, timeout: float = 10.0) -> dict[str, object]:
    executable = shutil.which(argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if not executable:
        return {"available": False, "argv": list(argv), "output": ""}
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=_subprocess_environment(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "argv": list(argv), "error": str(exc), "output": ""}
    output = completed.stdout[:8192].decode("utf-8", "replace").strip()
    return {
        "available": True,
        "argv": list(argv),
        "output": output,
        "returncode": completed.returncode,
    }


def tool_versions(root: Path, python: str) -> dict[str, object]:
    return {
        "dbus_run_session": _tool_output(["dbus-run-session", "--version"], cwd=root),
        "git": _tool_output(["git", "--version"], cwd=root),
        "gtk_vte": _tool_output(
            [
                python,
                "-P",
                "-B",
                "-c",
                (
                    "import gi;gi.require_version('Gtk','3.0');"
                    "gi.require_version('Vte','2.91');"
                    "from gi.repository import Gtk,Vte;"
                    "print('GTK %s.%s; VTE %s.%s' % "
                    "(Gtk.get_major_version(),Gtk.get_minor_version(),"
                    "Vte.get_major_version(),Vte.get_minor_version()))"
                ),
            ],
            cwd=root,
        ),
        "platform": platform.platform(),
        "pytest": _tool_output([python, "-P", "-B", "-m", "pytest", "--version"], cwd=root),
        "python": {"executable": python, "version": sys.version},
        "tmux": _tool_output(["tmux", "-V"], cwd=root),
        "xvfb_run": _tool_output(["xvfb-run", "--help"], cwd=root),
    }


def _bounded_os_read(descriptor: int, maximum_bytes: int) -> bytes:
    """Read at most one byte beyond a fixed protocol limit."""

    chunks: list[bytes] = []
    remaining = int(maximum_bytes) + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > maximum_bytes:
        raise ValidationError("validation runtime owner lease is oversized")
    return content


def _process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValidationError("validation runtime owner PID is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(proc_root / str(pid) / "stat", flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError("validation runtime owner identity is unavailable") from exc
    try:
        raw = _bounded_os_read(descriptor, 16 * 1024).decode("latin-1")
    finally:
        os.close(descriptor)
    close = raw.rfind(") ")
    fields = raw[close + 2 :].split() if close >= 0 else []
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise ValidationError("validation runtime owner stat is malformed") from exc
    if start_ticks <= 0:
        raise ValidationError("validation runtime owner start time is invalid")
    return start_ticks


def _current_boot_id(*, proc_root: Path = Path("/proc")) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(proc_root / "sys/kernel/random/boot_id", flags)
    except OSError as exc:
        raise ValidationError("Linux boot identity is unavailable") from exc
    try:
        value = _bounded_os_read(descriptor, 128).decode("ascii").strip().lower()
    except UnicodeError as exc:
        raise ValidationError("Linux boot identity is malformed") from exc
    finally:
        os.close(descriptor)
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
    ):
        raise ValidationError("Linux boot identity is malformed")
    return value


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return bool(
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_uid == right.st_uid
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
    )


def _safe_task_runtime_base(base: Path) -> Path:
    try:
        resolved = base.resolve(strict=True)
        metadata = base.lstat()
    except OSError as exc:
        raise ValidationError("short task runtime base is unavailable") from exc
    conventional_tmp = bool(
        metadata.st_mode & stat.S_ISVTX and metadata.st_mode & stat.S_IWOTH
    )
    private_base = bool(
        metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o700
    )
    if (
        resolved != base
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not (conventional_tmp or private_base)
    ):
        raise ValidationError("short task runtime base is unsafe")
    return resolved


def _private_task_runtime_base(base: Path, *, name: str) -> Path:
    try:
        resolved = base.resolve(strict=True)
        metadata = base.lstat()
    except OSError as exc:
        raise ValidationError(f"{name} is unavailable") from exc
    if (
        resolved != base
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValidationError(f"{name} is not a private 0700 directory")
    return resolved


def _runtime_base_fits_path_budget(base: Path) -> bool:
    return (
        len(os.fsencode(base / _TASK_RUNTIME_PATH_SAMPLE))
        <= _TASK_RUNTIME_MAX_PATH_BYTES
    )


def _ensure_private_task_runtime_base(parent: Path, name: str) -> Path:
    candidate = parent / name
    created = False
    try:
        candidate.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValidationError("portable validation runtime base cannot be created") from exc
    if created:
        try:
            candidate.chmod(0o700)
        except OSError as exc:
            raise ValidationError(
                "portable validation runtime base cannot be secured"
            ) from exc
    return _private_task_runtime_base(
        candidate,
        name="portable validation runtime base",
    )


def _portable_task_runtime_base() -> Path:
    raw_xdg = str(os.environ.get("XDG_RUNTIME_DIR", "") or "").strip()
    if raw_xdg:
        xdg = Path(raw_xdg).expanduser()
        if xdg.is_absolute():
            try:
                trusted_xdg = _private_task_runtime_base(
                    xdg,
                    name="XDG_RUNTIME_DIR",
                )
            except ValidationError:
                trusted_xdg = None
            if trusted_xdg is not None:
                candidate = trusted_xdg / _TASK_RUNTIME_PORTABLE_NAME
                if _runtime_base_fits_path_budget(candidate):
                    return _ensure_private_task_runtime_base(
                        trusted_xdg,
                        _TASK_RUNTIME_PORTABLE_NAME,
                    )

    fallback_parent = _safe_task_runtime_base(_TASK_RUNTIME_BASE)
    fallback_metadata = fallback_parent.lstat()
    if stat.S_IMODE(fallback_metadata.st_mode) != 0o1777:
        raise ValidationError(
            "portable validation runtime fallback parent is not mode 1777"
        )
    fallback_name = f"{_TASK_RUNTIME_FALLBACK_PREFIX}{os.geteuid()}"
    candidate = fallback_parent / fallback_name
    if not _runtime_base_fits_path_budget(candidate):
        raise ValidationError("portable validation runtime exceeds the path-length budget")
    return _ensure_private_task_runtime_base(fallback_parent, fallback_name)


def _configured_task_runtime_base() -> Path:
    raw = str(os.environ.get(_TASK_RUNTIME_BASE_ENV, "") or "").strip()
    if not raw:
        return _portable_task_runtime_base()
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise ValidationError(
            f"{_TASK_RUNTIME_BASE_ENV} must be an absolute path"
        )
    return _private_task_runtime_base(
        configured,
        name=_TASK_RUNTIME_BASE_ENV,
    )


def _production_task_runtime_base() -> Path:
    raw = str(os.environ.get(_TASK_RUNTIME_BASE_ENV, "") or "").strip()
    if not raw:
        raise ValidationError(
            f"{_TASK_RUNTIME_BASE_ENV} is required for production smokes"
        )
    base = _configured_task_runtime_base()
    try:
        from termdeck.storage_fabric import require_hot_storage

        require_hot_storage(
            managed_root=base,
            hot_paths={
                "auth_root": base,
                "checkpoint_root": base,
                "database_root": base,
                "runtime_root": base,
                "tmux_runtime": base,
            },
            production=True,
        )
    except Exception as exc:
        raise ValidationError(
            f"{_TASK_RUNTIME_BASE_ENV} is not verified Warp Drives SSD storage: {base}"
        ) from exc
    return base


def _validate_runtime_lease_payload(
    payload: object,
    *,
    directory: os.stat_result,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _TASK_RUNTIME_LEASE_KEYS:
        raise ValidationError("validation runtime owner lease fields are invalid")
    if payload.get("schema") != _TASK_RUNTIME_LEASE_SCHEMA:
        raise ValidationError("validation runtime owner lease schema is invalid")
    for key in ("pid", "start_ticks", "uid", "device", "inode", "created_ns"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"validation runtime owner lease {key} is invalid")
    if int(payload["pid"]) <= 1 or int(payload["start_ticks"]) <= 0:
        raise ValidationError("validation runtime owner process identity is invalid")
    boot_id = payload.get("boot_id")
    if not isinstance(boot_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        boot_id,
    ):
        raise ValidationError("validation runtime owner boot identity is invalid")
    if (
        int(payload["uid"]) != directory.st_uid
        or int(payload["device"]) != directory.st_dev
        or int(payload["inode"]) != directory.st_ino
    ):
        raise ValidationError("validation runtime owner lease does not bind its directory")
    return dict(payload)


def _read_task_runtime_lease(path: Path) -> tuple[dict[str, object], os.stat_result]:
    """Read a direct canonical owner lease through no-follow descriptors."""

    try:
        named = path.lstat()
    except OSError as exc:
        raise ValidationError("validation runtime cannot be inspected") from exc
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o700
    ):
        raise ValidationError("validation runtime directory is unsafe")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(path, directory_flags)
    except OSError as exc:
        raise ValidationError("validation runtime directory cannot be opened") from exc
    try:
        opened_directory = os.fstat(directory_fd)
        if not _same_directory_identity(named, opened_directory):
            raise ValidationError("validation runtime directory changed during open")
        lease_flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lease_fd = os.open(_TASK_RUNTIME_LEASE_NAME, lease_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ValidationError("validation runtime owner lease cannot be opened") from exc
        try:
            lease_metadata = os.fstat(lease_fd)
            if (
                not stat.S_ISREG(lease_metadata.st_mode)
                or lease_metadata.st_uid != os.geteuid()
                or lease_metadata.st_nlink != 1
                or stat.S_IMODE(lease_metadata.st_mode) != 0o600
                or lease_metadata.st_size < 0
                or lease_metadata.st_size > _TASK_RUNTIME_MAX_LEASE_BYTES
            ):
                raise ValidationError("validation runtime owner lease is unsafe")
            content = _bounded_os_read(lease_fd, _TASK_RUNTIME_MAX_LEASE_BYTES)
            if len(content) != lease_metadata.st_size:
                raise ValidationError("validation runtime owner lease changed during read")
            after_lease = os.fstat(lease_fd)
            if (
                after_lease.st_dev != lease_metadata.st_dev
                or after_lease.st_ino != lease_metadata.st_ino
                or after_lease.st_size != lease_metadata.st_size
                or after_lease.st_mtime_ns != lease_metadata.st_mtime_ns
            ):
                raise ValidationError("validation runtime owner lease changed during read")
        finally:
            os.close(lease_fd)
        payload = strict_json_loads(content)
        if canonical_json_bytes(payload) != content:
            raise ValidationError("validation runtime owner lease is not canonical")
        validated = _validate_runtime_lease_payload(payload, directory=opened_directory)
        final_directory = os.fstat(directory_fd)
        if not _same_directory_identity(opened_directory, final_directory):
            raise ValidationError("validation runtime directory changed during lease read")
        return validated, final_directory
    finally:
        os.close(directory_fd)


def _runtime_owner_alive(lease: Mapping[str, object]) -> bool:
    # PID alone is reusable. Boot identity and process start ticks make the
    # owner proof stable across both PID reuse and host reboot.
    if str(lease["boot_id"]) != _current_boot_id():
        return False
    current_start = _process_start_ticks(int(lease["pid"]))
    return current_start is not None and current_start == int(lease["start_ticks"])


def _quarantine_remove_task_runtime(
    path: Path,
    *,
    expected: os.stat_result,
) -> dict[str, object]:
    """Rename one authenticated inode before any recursive removal."""

    quarantine = path.parent / f"t1v-q-{uuid.uuid4().hex}"
    result: dict[str, object] = {
        "removed": False,
        "state": "failed",
        "quarantine": str(quarantine),
    }
    try:
        os.rename(path, quarantine)
    except FileNotFoundError:
        result.update({"removed": True, "state": "already-absent"})
        return result
    except OSError as exc:
        result["error"] = f"runtime quarantine rename failed: {exc}"
        return result
    try:
        observed = quarantine.lstat()
    except OSError as exc:
        result["error"] = f"runtime quarantine cannot be inspected: {exc}"
        return result
    if not _same_directory_identity(expected, observed):
        result["error"] = "runtime identity changed during quarantine rename"
    else:
        try:
            if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
                _make_quarantined_runtime_tree_removable(quarantine)
                shutil.rmtree(quarantine)
            else:
                quarantine.unlink()
        except OSError as exc:
            result["error"] = f"runtime quarantine cleanup failed: {exc}"
        else:
            result.update({"removed": True, "state": "removed"})
            return result
    if not os.path.lexists(path):
        try:
            os.rename(quarantine, path)
            result["state"] = "restored"
        except OSError:
            result["state"] = "quarantined"
    else:
        result["state"] = "quarantined"
    return result


def _make_quarantined_runtime_tree_removable(root: Path) -> None:
    """Open only owned directories inside an authenticated runtime for removal.

    Release verification intentionally freezes its disposable managed-release
    directories. Once the enclosing task runtime has been authenticated and
    renamed into its private quarantine, those read-only descendants must not
    make cleanup fail after the task itself has already succeeded. This walk
    changes permissions only on owned directories below that quarantined root;
    it never follows directory symlinks or broadens the cleanup target.
    """

    def _raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, _files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        current_path = Path(current)
        current_metadata = current_path.lstat()
        if (
            stat.S_ISLNK(current_metadata.st_mode)
            or not stat.S_ISDIR(current_metadata.st_mode)
            or current_metadata.st_uid != os.geteuid()
        ):
            raise PermissionError(
                f"quarantined runtime directory is not owned by the validator: {current_path}"
            )
        current_mode = stat.S_IMODE(current_metadata.st_mode)
        required_mode = current_mode | stat.S_IWUSR | stat.S_IXUSR
        if required_mode != current_mode:
            current_path.chmod(required_mode)

        # os.walk enters children after yielding the parent. Make owned
        # children traversable before that happens, while excluding symlinks
        # from both mutation and recursion.
        for name in tuple(directories):
            child = current_path / name
            child_metadata = child.lstat()
            if stat.S_ISLNK(child_metadata.st_mode):
                directories.remove(name)
                continue
            if not stat.S_ISDIR(child_metadata.st_mode):
                directories.remove(name)
                continue
            if child_metadata.st_uid != os.geteuid():
                raise PermissionError(
                    f"quarantined runtime directory is not owned by the validator: {child}"
                )
            child_mode = stat.S_IMODE(child_metadata.st_mode)
            child_required_mode = child_mode | stat.S_IWUSR | stat.S_IXUSR
            if child_required_mode != child_mode:
                child.chmod(child_required_mode)


def _task_runtime_reaper_cursor(
    runtime_base: Path,
) -> tuple[tuple[str, int, int], Any, bool]:
    try:
        metadata = runtime_base.lstat()
    except OSError as exc:
        raise ValidationError("short task runtime base cannot be scanned") from exc
    key = (str(runtime_base), int(metadata.st_dev), int(metadata.st_ino))
    for stale_key in tuple(_TASK_RUNTIME_REAPER_CURSORS):
        if stale_key[0] == key[0] and stale_key != key:
            _TASK_RUNTIME_REAPER_CURSORS.pop(stale_key).close()
    iterator = _TASK_RUNTIME_REAPER_CURSORS.pop(key, None)
    continued = iterator is not None
    if iterator is None:
        try:
            iterator = os.scandir(runtime_base)
        except OSError as exc:
            raise ValidationError("short task runtime base cannot be scanned") from exc
        while len(_TASK_RUNTIME_REAPER_CURSORS) >= _TASK_RUNTIME_MAX_REAPER_CURSORS:
            oldest_key = next(iter(_TASK_RUNTIME_REAPER_CURSORS))
            _TASK_RUNTIME_REAPER_CURSORS.pop(oldest_key).close()
    _TASK_RUNTIME_REAPER_CURSORS[key] = iterator
    return key, iterator, continued


def _owned_task_runtime_metadata(path: Path) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError("malformed validation runtime cannot be inspected") from exc
    if metadata.st_uid != os.geteuid():
        return None
    return metadata


def _reap_stale_task_runtimes(
    *,
    base: Path | None = None,
    max_entries: int = _TASK_RUNTIME_MAX_SCAN_ENTRIES,
    max_removals: int = _TASK_RUNTIME_MAX_REMOVALS,
) -> dict[str, object]:
    """Recover a bounded number of lease-bound dead validation runtimes."""

    runtime_base = _private_task_runtime_base(
        _configured_task_runtime_base() if base is None else Path(base),
        name="short task runtime base",
    )
    entry_limit = max(0, min(int(max_entries), _TASK_RUNTIME_MAX_SCAN_ENTRIES))
    removal_limit = max(0, min(int(max_removals), _TASK_RUNTIME_MAX_REMOVALS))
    receipt: dict[str, object] = {
        "base": str(runtime_base),
        "cursor_continued": False,
        "entry_bound_reached": False,
        "failed": 0,
        "inspected_entries": 0,
        "removed": 0,
        "removed_malformed": 0,
        "removal_bound_reached": False,
        "retained_live": 0,
        "retained_unsafe": 0,
        "scan_cycle_complete": False,
        "scanned_entries": 0,
    }
    if entry_limit == 0:
        receipt["entry_bound_reached"] = True
        return receipt
    with _TASK_RUNTIME_REAPER_CURSOR_LOCK:
        cursor_key, iterator, cursor_continued = _task_runtime_reaper_cursor(
            runtime_base
        )
        receipt["cursor_continued"] = cursor_continued
        while int(receipt["scanned_entries"]) < entry_limit:
            try:
                entry = next(iterator)
            except StopIteration:
                iterator.close()
                _TASK_RUNTIME_REAPER_CURSORS.pop(cursor_key, None)
                receipt["scan_cycle_complete"] = True
                break
            except OSError as exc:
                iterator.close()
                _TASK_RUNTIME_REAPER_CURSORS.pop(cursor_key, None)
                raise ValidationError(
                    "short task runtime base cannot be scanned"
                ) from exc
            receipt["scanned_entries"] = int(receipt["scanned_entries"]) + 1
            if (
                _TASK_RUNTIME_NAME.fullmatch(entry.name) is None
                and _TASK_RUNTIME_QUARANTINE_NAME.fullmatch(entry.name) is None
            ):
                continue
            receipt["inspected_entries"] = int(receipt["inspected_entries"]) + 1
            candidate = runtime_base / entry.name
            malformed = _TASK_RUNTIME_QUARANTINE_NAME.fullmatch(entry.name) is not None
            metadata: os.stat_result | None = None
            try:
                if not malformed:
                    lease, metadata = _read_task_runtime_lease(candidate)
                    if _runtime_owner_alive(lease):
                        receipt["retained_live"] = int(receipt["retained_live"]) + 1
                        continue
            except ValidationError:
                malformed = True
            if malformed:
                try:
                    metadata = _owned_task_runtime_metadata(candidate)
                except ValidationError:
                    receipt["failed"] = int(receipt["failed"]) + 1
                    continue
                if metadata is None:
                    receipt["retained_unsafe"] = int(receipt["retained_unsafe"]) + 1
                    continue
            if metadata is None:
                receipt["retained_unsafe"] = int(receipt["retained_unsafe"]) + 1
                continue
            if int(receipt["removed"]) >= removal_limit:
                receipt["removal_bound_reached"] = True
                if malformed:
                    receipt["retained_unsafe"] = int(receipt["retained_unsafe"]) + 1
                continue
            removal = _quarantine_remove_task_runtime(candidate, expected=metadata)
            if removal.get("removed") is True:
                receipt["removed"] = int(receipt["removed"]) + 1
                if malformed:
                    receipt["removed_malformed"] = int(
                        receipt["removed_malformed"]
                    ) + 1
            else:
                receipt["failed"] = int(receipt["failed"]) + 1
        if int(receipt["scanned_entries"]) >= entry_limit:
            receipt["entry_bound_reached"] = True
    return receipt


def _create_short_task_runtime(
    work: Path,
    *,
    base: Path | None = None,
) -> _TaskRuntime:
    """Create one lease-bound short runtime suitable for desktop IPC."""

    runtime_base = _private_task_runtime_base(
        _configured_task_runtime_base() if base is None else Path(base),
        name="short task runtime base",
    )
    prefix = f"t1v-{sha256_bytes(os.fsencode(str(work)))[:8]}-"
    path: Path | None = None
    try:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=runtime_base))
        path.chmod(0o700)
        metadata = path.lstat()
    except OSError as exc:
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)
        raise ValidationError("short task runtime cannot be created") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        shutil.rmtree(path, ignore_errors=True)
        raise ValidationError("short task runtime has unsafe ownership or mode")
    if len(os.fsencode(path)) > _TASK_RUNTIME_MAX_PATH_BYTES:
        shutil.rmtree(path, ignore_errors=True)
        raise ValidationError("short task runtime exceeds the path-length budget")

    owner_pid = os.getpid()
    owner_start_ticks = _process_start_ticks(owner_pid)
    if owner_start_ticks is None:
        shutil.rmtree(path, ignore_errors=True)
        raise ValidationError("validation runtime owner disappeared during creation")
    boot_id = _current_boot_id()
    lease_path = path / _TASK_RUNTIME_LEASE_NAME
    lease = {
        "boot_id": boot_id,
        "created_ns": time.time_ns(),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "pid": owner_pid,
        "schema": _TASK_RUNTIME_LEASE_SCHEMA,
        "start_ticks": owner_start_ticks,
        "uid": metadata.st_uid,
    }
    try:
        atomic_write_json(lease_path, lease, mode=0o600)
        validated, final_metadata = _read_task_runtime_lease(path)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    if validated != lease or not _same_directory_identity(metadata, final_metadata):
        shutil.rmtree(path, ignore_errors=True)
        raise ValidationError("validation runtime owner lease changed during creation")
    return _TaskRuntime(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        owner_pid=owner_pid,
        owner_start_ticks=owner_start_ticks,
        boot_id=boot_id,
        lease_path=lease_path,
    )


def _cleanup_task_runtime(runtime: _TaskRuntime | None) -> dict[str, object]:
    if runtime is None:
        return {"cleanup_ok": True, "external": False}
    receipt: dict[str, object] = {
        "cleanup_ok": False,
        "external": True,
        "path": str(runtime.path),
        "path_bytes": len(os.fsencode(runtime.path)),
        "owner_pid": runtime.owner_pid,
        "owner_start_ticks": runtime.owner_start_ticks,
        "boot_id": runtime.boot_id,
    }
    try:
        metadata = runtime.path.lstat()
    except FileNotFoundError:
        receipt.update({"already_absent": True, "cleanup_ok": True})
        return receipt
    except OSError as exc:
        receipt["error"] = f"cannot inspect runtime before cleanup: {exc}"
        return receipt
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != runtime.device
        or metadata.st_ino != runtime.inode
        or metadata.st_uid != runtime.uid
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        receipt["error"] = "runtime identity, ownership, type, or mode changed"
        return receipt
    try:
        lease, lease_directory = _read_task_runtime_lease(runtime.path)
    except ValidationError as exc:
        receipt["error"] = str(exc)
        return receipt
    if (
        int(lease["pid"]) != runtime.owner_pid
        or int(lease["start_ticks"]) != runtime.owner_start_ticks
        or str(lease["boot_id"]) != runtime.boot_id
        or not _same_directory_identity(metadata, lease_directory)
    ):
        receipt["error"] = "runtime owner lease no longer matches the task runtime"
        return receipt
    removal = _quarantine_remove_task_runtime(runtime.path, expected=lease_directory)
    receipt["removal"] = removal
    if removal.get("removed") is True:
        receipt["cleanup_ok"] = True
    else:
        receipt["error"] = str(removal.get("error") or "runtime cleanup failed")
    return receipt


def _task_environment(
    root: Path,
    work: Path,
    *,
    environment: str,
    profile: str | None = None,
) -> tuple[dict[str, str], _TaskRuntime | None]:
    env = _subprocess_environment(os.environ)
    env.pop(_TASK_RUNTIME_POLICY_ENV, None)
    env.update(
        {
            "NO_AT_BRIDGE": "1",
            "PYTHONBREAKPOINT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.pathsep.join((str(root / "scripts"), str(root))),
        }
    )
    runtime: _TaskRuntime | None = None
    try:
        if environment != "production-host":
            runtime_base = None
            if environment == "xvfb-dbus":
                if profile not in _PROFILES:
                    raise ValidationError(
                        "xvfb-dbus tasks require an explicit validation profile"
                    )
                runtime_base = (
                    _production_task_runtime_base()
                    if profile == "production"
                    else None
                )
                for key in (
                    "DBUS_SESSION_BUS_ADDRESS",
                    "DBUS_SESSION_BUS_PID",
                    "DBUS_SESSION_BUS_WINDOWID",
                ):
                    env.pop(key, None)
            runtime = _create_short_task_runtime(work, base=runtime_base)
            cache = runtime.path / "c"
            temporary = runtime.path / "t"
            pycache = runtime.path / "p"
            if environment == "xvfb-dbus":
                env[_TASK_RUNTIME_BASE_ENV] = str(runtime.path)
                env[_TASK_RUNTIME_POLICY_ENV] = str(profile)
            for directory in (cache, temporary, pycache):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            env.update(
                {
                    "PYTHONPYCACHEPREFIX": str(pycache),
                    "TMUX_TMPDIR": str(runtime.path),
                    "TMPDIR": str(temporary),
                    "XDG_CACHE_HOME": str(cache),
                    "XDG_RUNTIME_DIR": str(runtime.path),
                }
            )
    except BaseException as exc:
        cleanup = _cleanup_task_runtime(runtime)
        if cleanup.get("cleanup_ok") is not True:
            raise ValidationError(
                f"task environment setup failed and runtime cleanup was unsafe: {cleanup.get('error', 'unknown error')}"
            ) from exc
        raise
    return env, runtime


def _process_group_exists(pgid: int) -> bool:
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_process_group_exit(pgid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while _process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _kill_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    """Terminate the isolated process group, including writers surviving its leader."""

    pgid = process.pid
    grace = max(0.05, float(grace_seconds))
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    except OSError:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    _wait_for_process_group_exit(pgid, grace)

    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        _wait_for_process_group_exit(pgid, grace)

    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def _drain_bounded_stream(
    stream,
    path: Path,
    *,
    limit: int,
    overflow: threading.Event,
    stop: threading.Event,
    result: dict[str, object],
) -> None:
    """Drain one child pipe without waiting for EOF before retaining short output."""

    observed = 0
    completed = False
    error = ""
    stop_deadline: float | None = None
    try:
        descriptor = stream.fileno()
        # BufferedReader.read(65536) may wait for EOF after a short write when a
        # descendant inherits the pipe. Nonblocking os.read retains available
        # bytes immediately, while select keeps the reader bounded and stoppable.
        os.set_blocking(descriptor, False)
        with path.open("xb", buffering=0) as handle:
            os.chmod(path, 0o600)
            while True:
                if stop.is_set() and stop_deadline is None:
                    stop_deadline = time.monotonic() + 0.25
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    break
                timeout = 0.0 if stop_deadline is not None else 0.1
                try:
                    readable, _, _ = select.select([descriptor], [], [], timeout)
                except (OSError, ValueError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break
                if not readable:
                    if stop_deadline is not None:
                        break
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break
                if not chunk:
                    completed = True
                    break
                observed += len(chunk)
                result["observed_bytes"] = observed
                remaining = max(0, limit - handle.tell())
                if remaining:
                    handle.write(chunk[:remaining])
                if observed > limit:
                    overflow.set()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        result["observed_bytes"] = observed
        result["completed"] = completed
        result["error"] = error


def _join_output_readers(
    readers: Sequence[threading.Thread], timeout_seconds: float
) -> list[threading.Thread]:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    for reader in readers:
        remaining = max(0.0, deadline - time.monotonic())
        reader.join(timeout=remaining)
    return [reader for reader in readers if reader.is_alive()]


def run_process(
    *,
    task_id: str,
    kind: str,
    relative: str,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    output_root: Path,
    timeout_seconds: int,
    grace_seconds: int,
    stdout_limit: int,
    stderr_limit: int,
    cleanup_runtime: _TaskRuntime | None = None,
) -> dict[str, object]:
    task_root = output_root / kind / task_id
    task_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    task_root.chmod(0o700)
    stdout_path = task_root / "stdout"
    stderr_path = task_root / "stderr"
    started_ns = time.time_ns()
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    start_error = ""
    output_limit_exceeded = False
    output_group_cleanup_required = False
    stdout_observed: dict[str, object] = {
        "observed_bytes": 0,
        "completed": False,
        "error": "",
    }
    stderr_observed: dict[str, object] = {
        "observed_bytes": 0,
        "completed": False,
        "error": "",
    }
    overflow = threading.Event()
    stop_readers = threading.Event()
    readers: list[threading.Thread] = []
    reader_names_alive: list[str] = []
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=_subprocess_environment(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(
                target=_drain_bounded_stream,
                args=(process.stdout, stdout_path),
                kwargs={
                    "limit": stdout_limit,
                    "overflow": overflow,
                    "stop": stop_readers,
                    "result": stdout_observed,
                },
                name=f"validation-{task_id}-stdout",
                daemon=False,
            ),
            threading.Thread(
                target=_drain_bounded_stream,
                args=(process.stderr, stderr_path),
                kwargs={
                    "limit": stderr_limit,
                    "overflow": overflow,
                    "stop": stop_readers,
                    "result": stderr_observed,
                },
                name=f"validation-{task_id}-stderr",
                daemon=False,
            ),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if overflow.is_set():
                output_limit_exceeded = True
                _kill_process_group(process, grace_seconds)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(process, grace_seconds)
                break
            overflow.wait(0.025)
        if process.poll() is None:
            _kill_process_group(process, grace_seconds)
        else:
            process.wait()
    except BaseException as exc:
        if process is not None:
            _kill_process_group(process, grace_seconds)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        start_error = f"{type(exc).__name__}: {exc}"
    finally:
        drain_grace = max(1.0, float(grace_seconds))
        alive = _join_output_readers(readers, drain_grace)
        if alive and process is not None:
            output_group_cleanup_required = True
            _kill_process_group(process, grace_seconds)
            alive = _join_output_readers(alive, drain_grace)
        stop_readers.set()
        if alive:
            alive = _join_output_readers(alive, drain_grace)
        if alive and process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            alive = _join_output_readers(alive, drain_grace)
        elif process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        reader_names_alive = [reader.name for reader in alive]
        for path in (stdout_path, stderr_path):
            if not path.exists():
                path.write_bytes(b"")
                path.chmod(0o600)
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    stdout_size = stdout_path.stat().st_size
    stderr_size = stderr_path.stat().st_size
    returncode = None if process is None else process.returncode
    signal_number = -returncode if isinstance(returncode, int) and returncode < 0 else None
    stdout_observed_size = int(stdout_observed.get("observed_bytes", 0) or 0)
    stderr_observed_size = int(stderr_observed.get("observed_bytes", 0) or 0)
    output_limit_exceeded = bool(
        output_limit_exceeded
        or overflow.is_set()
        or stdout_observed_size > stdout_limit
        or stderr_observed_size > stderr_limit
    )
    output_drain_errors = [
        str(error)
        for error in (stdout_observed.get("error"), stderr_observed.get("error"))
        if error
    ]
    output_drain_complete = bool(
        not reader_names_alive
        and stdout_observed.get("completed") is True
        and stderr_observed.get("completed") is True
        and not output_drain_errors
    )
    runtime_receipt = _cleanup_task_runtime(cleanup_runtime)
    ok = bool(
        not start_error
        and not timed_out
        and returncode == 0
        and not output_limit_exceeded
        and output_drain_complete
        and stdout_size <= stdout_limit
        and stderr_size <= stderr_limit
        and runtime_receipt.get("cleanup_ok") is True
    )
    return {
        "argv": list(argv),
        "duration_ms": duration_ms,
        "ended_ns": time.time_ns(),
        "environment_class": "production-host" if kind == "smoke-host" else "isolated",
        "kind": kind,
        "ok": ok,
        "output_drain": {
            "alive_readers": reader_names_alive,
            "complete": output_drain_complete,
            "errors": output_drain_errors,
            "group_cleanup_required": output_group_cleanup_required,
        },
        "output_limit_exceeded": output_limit_exceeded,
        "path": relative,
        "returncode": returncode,
        "runtime": runtime_receipt,
        "signal": signal_number,
        "start_error": start_error,
        "started_ns": started_ns,
        "stderr": {
            "observed_size": stderr_observed_size,
            "path": str(stderr_path.relative_to(output_root)),
            "sha256": sha256_file(stderr_path),
            "size": stderr_size,
        },
        "stdout": {
            "observed_size": stdout_observed_size,
            "path": str(stdout_path.relative_to(output_root)),
            "sha256": sha256_file(stdout_path),
            "size": stdout_size,
        },
        "task_id": task_id,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
    }

def _read_capped(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise ValidationError(f"output exceeds read limit: {path}: {size} > {limit}")
    return path.read_bytes()


def parse_test_summary(text: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for key, pattern in _TEST_SUMMARY_PATTERNS.items():
        matches = [int(value) for value in pattern.findall(text)]
        totals[key] = matches[-1] if matches else 0
    if (
        totals["passed"]
        + totals["skipped"]
        + totals["subtests_passed"]
        + totals["xfailed"]
        + totals["xpassed"]
        == 0
    ):
        raise ValidationError("pytest output has no parseable test outcome")
    return totals


def parse_smoke_output(data: bytes, *, limit: int) -> dict[str, object]:
    if len(data) > limit:
        raise ValidationError("smoke JSON exceeds its configured limit")
    payload = strict_json_loads(data)
    if not isinstance(payload, dict):
        raise ValidationError("smoke stdout must be one JSON object")
    if payload.get("ok") is not True:
        raise ValidationError("smoke JSON does not report ok=true")
    skipped = payload.get("skipped") is True
    reason = payload.get("reason")
    if skipped and (not isinstance(reason, str) or not reason.strip()):
        raise ValidationError("a skipped smoke must report a nonempty reason")
    summary: dict[str, object] = {"ok": True, "skipped": skipped}
    if isinstance(reason, str) and reason.strip():
        summary["reason"] = reason.strip()[:2000]
    return summary


def _build_smoke_argv(entry: Mapping[str, object], *, python: str, root: Path) -> list[str]:
    relative = str(entry["path"])
    script = str(root / relative)
    environment = entry["environment"]
    if environment == "production-host":
        return [python, "-P", "-B", script]
    xvfb = shutil.which("xvfb-run")
    dbus = shutil.which("dbus-run-session")
    if not xvfb or not dbus:
        raise ValidationError("portable GUI smokes require xvfb-run and dbus-run-session")
    return [
        xvfb,
        "-a",
        "-s",
        "-screen 0 1280x1024x24 -extension GLX",
        dbus,
        "--",
        python,
        "-P",
        "-B",
        script,
    ]


def _record_failure(failures: list[dict[str, object]], task: Mapping[str, object], reason: str) -> None:
    failures.append(
        {
            "kind": task.get("kind"),
            "path": task.get("path"),
            "reason": reason[:4000],
            "task_id": task.get("task_id"),
        }
    )


def run_validation(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    root = Path(args.root).expanduser().resolve(strict=True)
    output = _safe_output_root(Path(args.output))
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    started_ns = time.time_ns()
    receipt: dict[str, object] = {
        "completed": False,
        "created_ns": started_ns,
        "failures": [],
        "mode": "manifest"
        if args.check_manifest
        else "tests"
        if args.tests_only
        else "smokes"
        if args.smokes_only
        else "all",
        "ok": False,
        "profile": args.profile,
        "schema": _RECEIPT_SCHEMA,
    }
    failures: list[dict[str, object]] = []
    tasks: list[dict[str, object]] = []
    interrupted: BaseException | None = None
    try:
        manifest, raw_manifest, tests, smokes = load_manifest(root, manifest_path)
        selected_tests, selected_smokes = selected_inventory(
            manifest,
            profile=args.profile,
            tests_only=args.tests_only,
            smokes_only=args.smokes_only,
            discovered_tests=tests,
        )
        source_before = source_fingerprint(root, excluded_root=output)
        receipt.update(
            {
                "discovered": {"smokes": smokes, "tests": tests},
                "manifest": {
                    "path": str(manifest_path.relative_to(root)),
                    "schema": manifest["schema"],
                    "sha256": sha256_bytes(raw_manifest),
                },
                "selected": {
                    "smokes": [str(entry["path"]) for entry in selected_smokes],
                    "tests": selected_tests,
                },
                "source_before": source_before,
                "tools": {} if args.check_manifest else tool_versions(root, args.python),
            }
        )
        atomic_write_json(
            output / "running.json",
            {
                "created_ns": started_ns,
                "manifest_sha256": sha256_bytes(raw_manifest),
                "profile": args.profile,
                "schema": _RUNNING_SCHEMA,
                "selected": receipt["selected"],
                "source_before": source_before,
                "state": "running",
            },
        )
        if not args.check_manifest and (selected_tests or selected_smokes):
            runtime_preflight_base = (
                _production_task_runtime_base()
                if args.profile == "production" and selected_smokes
                else None
            )
            runtime_preflight = _reap_stale_task_runtimes(
                base=runtime_preflight_base
            )
            receipt["runtime_preflight"] = runtime_preflight
            if int(runtime_preflight.get("failed", 0)):
                raise ValidationError(
                    "short task runtime recovery could not complete an authenticated removal"
                )

        limits = manifest["limits"]
        assert isinstance(limits, dict)
        stdout_limit = int(limits["stdout_bytes"])
        stderr_limit = int(limits["stderr_bytes"])
        smoke_json_limit = int(limits["smoke_json_bytes"])
        grace = int(limits["termination_grace_seconds"])

        if not args.check_manifest and selected_tests:
            syntax_work = output / "work" / "syntax"
            syntax_work.mkdir(parents=True, mode=0o700)
            syntax_env, syntax_runtime = _task_environment(
                root, syntax_work, environment="isolated"
            )
            syntax_task = run_process(
                task_id="000-syntax",
                kind="syntax",
                relative="termdeck tests scripts",
                argv=[
                    args.python,
                    "-P",
                    "-B",
                    "-m",
                    "compileall",
                    "-q",
                    "termdeck",
                    "tests",
                    "scripts",
                ],
                cwd=root,
                env=syntax_env,
                output_root=output,
                timeout_seconds=min(300, int(manifest["tests"]["timeout_seconds"])),  # type: ignore[index]
                grace_seconds=grace,
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                cleanup_runtime=syntax_runtime,
            )
            tasks.append(syntax_task)
            if not syntax_task["ok"]:
                _record_failure(failures, syntax_task, "syntax compilation failed")

            test_timeout = int(manifest["tests"]["timeout_seconds"])  # type: ignore[index]
            for index, relative in enumerate(selected_tests, start=1):
                task_id = f"{index:03d}-{Path(relative).stem}"
                work = output / "work" / "tests" / task_id
                work.mkdir(parents=True, mode=0o700)
                env, task_runtime = _task_environment(
                    root, work, environment="isolated"
                )
                task = run_process(
                    task_id=task_id,
                    kind="test",
                    relative=relative,
                    argv=[
                        args.python,
                        "-P",
                        "-B",
                        "-m",
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        relative,
                    ],
                    cwd=root,
                    env=env,
                    output_root=output,
                    timeout_seconds=test_timeout,
                    grace_seconds=grace,
                    stdout_limit=stdout_limit,
                    stderr_limit=stderr_limit,
                    cleanup_runtime=task_runtime,
                )
                if task["ok"]:
                    try:
                        stdout_path = output / str(task["stdout"]["path"])  # type: ignore[index]
                        summary = parse_test_summary(
                            _read_capped(stdout_path, stdout_limit).decode("utf-8", "replace")
                        )
                        task["summary"] = summary
                    except ValidationError as exc:
                        task["ok"] = False
                        _record_failure(failures, task, str(exc))
                else:
                    _record_failure(failures, task, "test subprocess failed")
                tasks.append(task)

        if not args.check_manifest and selected_smokes:
            for index, entry in enumerate(selected_smokes, start=1):
                relative = str(entry["path"])
                task_id = f"{index:03d}-{Path(relative).stem}"
                environment = str(entry["environment"])
                work = output / "work" / "smokes" / task_id
                work.mkdir(parents=True, mode=0o700)
                env, task_runtime = _task_environment(
                    root,
                    work,
                    environment=environment,
                    profile=args.profile,
                )
                try:
                    argv = _build_smoke_argv(entry, python=args.python, root=root)
                    kind = "smoke-host" if environment == "production-host" else "smoke"
                    task = run_process(
                        task_id=task_id,
                        kind=kind,
                        relative=relative,
                        argv=argv,
                        cwd=root,
                        env=env,
                        output_root=output,
                        timeout_seconds=int(entry["timeout_seconds"]),
                        grace_seconds=grace,
                        stdout_limit=stdout_limit,
                        stderr_limit=stderr_limit,
                        cleanup_runtime=task_runtime,
                    )
                except ValidationError as exc:
                    runtime_receipt = _cleanup_task_runtime(task_runtime)
                    task = {
                        "argv": [],
                        "kind": "smoke",
                        "ok": False,
                        "path": relative,
                        "runtime": runtime_receipt,
                        "task_id": task_id,
                    }
                    _record_failure(failures, task, str(exc))
                    tasks.append(task)
                    continue
                if task["ok"]:
                    try:
                        stdout_path = output / str(task["stdout"]["path"])  # type: ignore[index]
                        task["summary"] = parse_smoke_output(
                            _read_capped(stdout_path, smoke_json_limit),
                            limit=smoke_json_limit,
                        )
                    except ValidationError as exc:
                        task["ok"] = False
                        _record_failure(failures, task, str(exc))
                else:
                    _record_failure(failures, task, "smoke subprocess failed")
                tasks.append(task)

        source_after = source_fingerprint(root, excluded_root=output)
        receipt["source_after"] = source_after
        if source_after != source_before:
            failures.append(
                {
                    "kind": "source",
                    "path": ".",
                    "reason": "source fingerprint changed during validation",
                    "task_id": "source-stability",
                }
            )
        aggregates = {
            "smokes": {
                "failed": sum(
                    task.get("kind") in {"smoke", "smoke-host"} and not task.get("ok")
                    for task in tasks
                ),
                "passed": sum(
                    task.get("kind") in {"smoke", "smoke-host"} and task.get("ok") is True
                    for task in tasks
                ),
                "selected": len(selected_smokes),
                "skipped": sum(
                    task.get("kind") in {"smoke", "smoke-host"}
                    and isinstance(task.get("summary"), dict)
                    and task["summary"].get("skipped") is True  # type: ignore[index]
                    for task in tasks
                ),
            },
            "tests": {
                "failed_shards": sum(task.get("kind") == "test" and not task.get("ok") for task in tasks),
                "selected_shards": len(selected_tests),
                "successful_shards": sum(task.get("kind") == "test" and task.get("ok") is True for task in tasks),
                "totals": {
                    key: sum(
                        int(task["summary"].get(key, 0))  # type: ignore[index]
                        for task in tasks
                        if task.get("kind") == "test" and isinstance(task.get("summary"), dict)
                    )
                    for key in _TEST_SUMMARY_PATTERNS
                },
            },
        }
        receipt.update(
            {
                "aggregates": aggregates,
                "completed": True,
                "completed_ns": time.time_ns(),
                "failures": failures,
                "ok": not failures,
                "tasks": tasks,
            }
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            interrupted = exc
        failures.append(
            {
                "kind": "runner",
                "path": str(args.manifest),
                "reason": f"{type(exc).__name__}: {exc}"[:4000],
                "task_id": "runner",
            }
        )
        receipt.update(
            {
                "completed": False,
                "completed_ns": time.time_ns(),
                "failures": failures,
                "ok": False,
                "tasks": tasks,
            }
        )
    atomic_write_json(output / "receipt.json", receipt)
    if interrupted is not None:
        raise interrupted
    return (0 if receipt.get("ok") is True else 1), receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--manifest", default="scripts/validation_manifest.json")
    parser.add_argument("--profile", choices=_PROFILES, default="ci")
    parser.add_argument("--output", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--tests-only", action="store_true")
    parser.add_argument("--smokes-only", action="store_true")
    parser.add_argument("--check-manifest", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tests_only and args.smokes_only:
        parser.error("--tests-only and --smokes-only are mutually exclusive")
    if args.check_manifest and (args.tests_only or args.smokes_only):
        parser.error("--check-manifest cannot be combined with execution modes")
    try:
        code, receipt = run_validation(args)
    except ValidationError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "completed": receipt.get("completed"),
                "failure_count": len(receipt.get("failures", [])),
                "ok": receipt.get("ok"),
                "profile": receipt.get("profile"),
                "receipt": str(Path(args.output).absolute() / "receipt.json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
