from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import stat
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fcntl

_SHARED_FILES = (
    "config.toml",
    "AGENTS.md",
    "AGENTS.override.md",
    "hooks.json",
    ".env",
)
_SHARED_DIRECTORIES = (
    "agents",
    "rules",
    "skills",
    "plugins",
    "memories",
    "memories_extensions",
    "hooks",
    "themes",
    "pets",
    "prompts",
)
_WRAPPER_MARKER = "# termdeck-private-codex-wrapper-v1"
_BASH_RC_MARKER = "# termdeck-private-bash-rc-v1"
_CANONICAL_HOME_BINDING = ".termdeck-canonical-home"
_CONFIG_BACKUP_ROOT = ".termdeck-config-backups"
_AUTH_SCOPE_PATTERN = re.compile(r"auth-[A-Za-z0-9_-]+\Z")
_MANAGED_CODEX_LAUNCHER = "codex-contained-user"
_MANAGED_CODEX_HELPER = "codex-contained-user.sh"

LauncherAttestor = Callable[[Path], bool]


class CodexConfigScopeError(RuntimeError):
    """Raised when a private Codex configuration boundary is unsafe."""


@dataclass(frozen=True)
class CodexConfigScopeStatus:
    """Result of reconciling one private ``CODEX_HOME``.

    Existing non-symlink configuration is never replaced here because it may
    still be in use by a live Codex process.  Callers can distinguish that
    migration gate from a launcher which is merely unavailable.
    """

    private_home: Path
    canonical_home: Path
    shared_paths: tuple[Path, ...]
    migration_required_paths: tuple[Path, ...]
    launcher_path: Path | None
    wrapper_path: Path | None
    canonical_available: bool = True
    canonical_home_in_use: bool = False

    @property
    def migration_required(self) -> bool:
        return bool(self.migration_required_paths)

    @property
    def configuration_ready(self) -> bool:
        return (
            self.canonical_available or self.canonical_home_in_use
        ) and not self.migration_required

    @property
    def launcher_ready(self) -> bool:
        return self.canonical_home_in_use or self.wrapper_path is not None

    @property
    def ready(self) -> bool:
        return self.configuration_ready and self.launcher_ready


@dataclass(frozen=True)
class CodexConfigMigrationResult:
    """Receipt for one quiescent legacy-configuration migration."""

    status: CodexConfigScopeStatus
    backup_path: Path | None
    migrated_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _ConfigurationPlan:
    binding_status: str
    assets: tuple[tuple[Path, Path, bool, str], ...]
    legacy_only_paths: tuple[Path, ...]
    wrapper_path: Path
    wrapper_status: str

    @property
    def migration_required_paths(self) -> tuple[Path, ...]:
        paths = [
            destination
            for _target, destination, _directory, status in self.assets
            if status == "legacy"
        ]
        paths.extend(self.legacy_only_paths)
        if self.wrapper_status == "legacy":
            paths.append(self.wrapper_path)
        return tuple(sorted(paths, key=str))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_termdeck_codex_wrapper(candidate: Path) -> bool:
    try:
        if not candidate.is_file():
            return False
        with candidate.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return _WRAPPER_MARKER.encode("utf-8") in head


def _validated_launcher(candidate: Path, private_home: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError:
        return None
    if (
        resolved.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or stat.S_IMODE(metadata.st_mode) & stat.S_IXUSR == 0
        or not os.access(resolved, os.X_OK)
    ):
        return None
    if _is_within(resolved, private_home) or _is_termdeck_codex_wrapper(resolved):
        return None
    return resolved


def _read_small_regular(path: Path, *, maximum: int) -> bytes | None:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_size > maximum
        ):
            return None
        return path.read_bytes()
    except OSError:
        return None


def _read_managed_helper(path: Path, *, maximum: int) -> bytes | None:
    try:
        link_metadata = path.lstat()
        if link_metadata.st_uid != os.geteuid():
            return None
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return _read_small_regular(resolved, maximum=maximum)


def _is_managed_codex_launcher(
    candidate: Path,
    resolved: Path,
    _search_home: Path,
) -> bool:
    """Recognize the installed contained launcher, not a same-named binary."""

    try:
        trusted_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError, RuntimeError):
        return False
    expected_entry = trusted_home / ".local" / "bin" / "codex"
    expected_target = trusted_home / ".local" / "bin" / _MANAGED_CODEX_LAUNCHER
    expected_helper = trusted_home / ".local" / "libexec" / _MANAGED_CODEX_HELPER
    try:
        if Path(os.path.abspath(candidate)) != Path(os.path.abspath(expected_entry)):
            return False
        if not candidate.is_symlink():
            return False
        if resolved != expected_target.resolve(strict=True):
            return False
    except (OSError, RuntimeError):
        return False
    launcher = _read_small_regular(expected_target, maximum=1024 * 1024)
    helper = _read_managed_helper(expected_helper, maximum=1024 * 1024)
    if launcher is None or helper is None:
        return False
    return (
        os.fsencode(expected_helper) in launcher
        and helper.startswith(b"#!/usr/bin/env bash\nset -Eeuo pipefail\n")
        and b"CODEX_SSD_WORKSPACE_HELPER" in helper
        and b"CODEX_RUNTIME_SCOPE_HELPER" in helper
        and b"vendor_runtime_root" in helper
    )


def _launcher_is_attested(
    candidate: Path,
    resolved: Path,
    user_home: Path,
    launcher_attestor: LauncherAttestor | None,
) -> bool:
    if _is_managed_codex_launcher(candidate, resolved, user_home):
        return True
    if launcher_attestor is None:
        return False
    try:
        return launcher_attestor(resolved) is True
    except Exception:
        return False


def resolve_codex_launcher(
    private_home: str | Path,
    *,
    search_path: str | None = None,
    user_home: str | Path | None = None,
    launcher_attestor: LauncherAttestor | None = None,
) -> Path | None:
    """Resolve an attested managed Codex launcher outside a private wrapper.

    The installed contained ``codex`` launcher has a narrow on-disk contract.
    ``TERMDECK_CODEX_BINARY`` may override it only when ``launcher_attestor``
    independently attests the resolved executable.  The injection seam exists
    for a release verifier and deterministic tests; a filename, PATH position,
    or environment override is never trust evidence by itself.
    """

    home = Path(private_home).expanduser().resolve(strict=False)
    fallback_home = (
        Path(user_home).expanduser()
        if user_home is not None
        else Path.home().expanduser()
    )
    configured = os.environ.get("TERMDECK_CODEX_BINARY", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise CodexConfigScopeError(
                "TERMDECK_CODEX_BINARY must be an absolute path"
            )
        executable = _validated_launcher(candidate, home)
        if executable is None:
            raise CodexConfigScopeError(
                f"TERMDECK_CODEX_BINARY is not a safe executable: {candidate}"
            )
        if not _launcher_is_attested(
            candidate,
            executable,
            fallback_home,
            launcher_attestor,
        ):
            raise CodexConfigScopeError(
                "TERMDECK_CODEX_BINARY is not an attested managed Codex launcher"
            )
        return executable

    # The desktop process often has only a system PATH even though the normal
    # interactive `codex` command is the user's contained launcher here.
    # Prefer that conventional launcher, then honor the inherited PATH.
    search_directories: list[Path] = [fallback_home / ".local" / "bin"]
    raw_path = os.environ.get("PATH", "") if search_path is None else search_path
    for raw_entry in raw_path.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        candidate_directory = Path(entry).expanduser()
        # Relative PATH entries make the generated absolute exec target depend
        # on the GUI's working directory.  They are not a stable host launcher.
        if not candidate_directory.is_absolute():
            continue
        search_directories.append(candidate_directory)

    search_directories.extend(
        (
            fallback_home / ".npm-global" / "bin",
            fallback_home / ".bun" / "bin",
            fallback_home / ".local" / "share" / "pnpm",
            Path("/usr/local/bin"),
            Path("/usr/bin"),
            Path("/bin"),
        )
    )

    seen: set[Path] = set()
    for directory in search_directories:
        directory = directory.resolve(strict=False)
        if directory in seen or _is_within(directory, home):
            continue
        seen.add(directory)
        executable = _validated_launcher(directory / "codex", home)
        if executable is not None and _launcher_is_attested(
            directory / "codex",
            executable,
            fallback_home,
            launcher_attestor,
        ):
            return executable
    return None


def _canonical_assets(source: Path) -> tuple[tuple[str, Path, bool], ...]:
    assets: list[tuple[str, Path, bool]] = []
    names: list[str] = list(_SHARED_FILES)
    names.extend(
        path.name
        for path in sorted(source.glob("*.config.toml"), key=lambda item: item.name)
        if path.name not in names
    )
    names.extend(name for name in _SHARED_DIRECTORIES if name not in names)

    for name in names:
        target = source / name
        if target.is_symlink() and not target.exists():
            raise CodexConfigScopeError(
                f"canonical Codex configuration asset is a broken symlink: {target}"
            )
        expects_directory = name in _SHARED_DIRECTORIES
        exists_as_file = target.is_file()
        exists_as_directory = target.is_dir()
        if expects_directory and exists_as_file:
            raise CodexConfigScopeError(
                f"canonical Codex directory asset is not a directory: {target}"
            )
        if not expects_directory and exists_as_directory:
            raise CodexConfigScopeError(
                f"canonical Codex file asset is not a regular file: {target}"
            )
        if exists_as_file:
            _validate_canonical_authority(target, directory=False)
            assets.append((name, target, False))
        elif exists_as_directory:
            _validate_canonical_authority(target, directory=True)
            assets.append((name, target, True))
        elif target.exists() or target.is_symlink():
            raise CodexConfigScopeError(
                f"canonical Codex configuration asset has an unsupported type: {target}"
            )
    return tuple(assets)


def _validate_canonical_authority(path: Path, *, directory: bool) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CodexConfigScopeError(
            f"canonical Codex configuration asset cannot be inspected: {path}"
        ) from exc
    expected = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if (
        not expected
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        kind = "directory" if directory else "file"
        raise CodexConfigScopeError(
            f"canonical Codex configuration {kind} has unsafe authority: {path}"
        )


def _inspect_destination(
    destination: Path,
    target: Path,
    *,
    expects_directory: bool,
) -> str:
    if destination.is_symlink():
        try:
            destination_target = destination.resolve(strict=True)
            canonical_target = target.resolve(strict=True)
        except OSError as exc:
            raise CodexConfigScopeError(
                f"private Codex configuration link cannot be resolved: {destination}"
            ) from exc
        if destination_target != canonical_target:
            raise CodexConfigScopeError(
                f"private Codex configuration link has an unexpected target: {destination}"
            )
        return "shared"
    if not destination.exists():
        return "missing"
    try:
        mode = destination.lstat().st_mode
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration path cannot be inspected: {destination}"
        ) from exc
    expected = stat.S_ISDIR(mode) if expects_directory else stat.S_ISREG(mode)
    if expected:
        return "legacy"
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
        kind = "directory" if expects_directory else "regular file"
        raise CodexConfigScopeError(
            f"private Codex configuration path is not a {kind}: {destination}"
        )
    raise CodexConfigScopeError(
        f"private Codex configuration path has an unsupported type: {destination}"
    )


def _validate_canonical_root(source: Path) -> None:
    try:
        metadata = source.stat()
    except OSError as exc:
        raise CodexConfigScopeError(
            f"canonical Codex home cannot be inspected safely: {source}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise CodexConfigScopeError(
            f"canonical Codex home has unsafe authority: {source}"
        )


def _inspect_canonical_binding(home: Path, source: Path) -> str:
    binding = home / _CANONICAL_HOME_BINDING
    if binding.is_symlink():
        try:
            metadata = binding.lstat()
            resolved = binding.resolve(strict=True)
        except OSError as exc:
            raise CodexConfigScopeError(
                f"private Codex canonical-home binding cannot be resolved: {binding}"
            ) from exc
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise CodexConfigScopeError(
                f"private Codex canonical-home binding is unsafe: {binding}"
            )
        if resolved != source:
            raise CodexConfigScopeError(
                f"private Codex canonical-home binding has an unexpected target: {binding}"
            )
        return "shared"
    if not binding.exists():
        return "missing"
    raise CodexConfigScopeError(
        f"private Codex canonical-home binding is not a symlink: {binding}"
    )


def _bound_canonical_home(home: Path) -> Path:
    binding = home / _CANONICAL_HOME_BINDING
    if not binding.is_symlink():
        raise CodexConfigScopeError(
            "private Codex home has no validated canonical configuration binding: "
            f"{binding}"
        )
    try:
        metadata = binding.lstat()
        source = binding.resolve(strict=True)
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex canonical-home binding cannot be resolved: {binding}"
        ) from exc
    if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
        raise CodexConfigScopeError(
            f"private Codex canonical-home binding is unsafe: {binding}"
        )
    if not source.is_dir() or source == home or home in source.parents:
        raise CodexConfigScopeError(
            f"private Codex canonical-home binding has an unsafe target: {binding}"
        )
    _validate_canonical_root(source)
    return source


def _legacy_only_configuration_paths(
    home: Path,
    canonical_names: set[str],
) -> tuple[Path, ...]:
    names: set[str] = set(_SHARED_FILES) | set(_SHARED_DIRECTORIES)
    try:
        names.update(path.name for path in home.glob("*.config.toml"))
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration profiles cannot be inspected: {home}"
        ) from exc

    legacy: list[Path] = []
    for name in sorted(names):
        if name in canonical_names:
            continue
        destination = home / name
        if destination.is_symlink():
            raise CodexConfigScopeError(
                "private Codex configuration link has no canonical target: "
                f"{destination}"
            )
        if not destination.exists():
            continue
        try:
            mode = destination.lstat().st_mode
        except OSError as exc:
            raise CodexConfigScopeError(
                f"private Codex configuration path cannot be inspected: {destination}"
            ) from exc
        expects_directory = name in _SHARED_DIRECTORIES
        expected = stat.S_ISDIR(mode) if expects_directory else stat.S_ISREG(mode)
        if not expected:
            kind = "directory" if expects_directory else "regular file"
            raise CodexConfigScopeError(
                f"private Codex configuration path is not a {kind}: {destination}"
            )
        legacy.append(destination)
    return tuple(legacy)


def _configuration_plan(home: Path, source: Path) -> _ConfigurationPlan:
    # Bind the authority before inspecting any per-asset link. A changed
    # source must be reported as an identity failure, not as a collection of
    # apparently mismatched assets.
    binding_status = _inspect_canonical_binding(home, source)
    assets = _canonical_assets(source)
    planned_assets: list[tuple[Path, Path, bool, str]] = []
    for name, target, directory in assets:
        destination = home / name
        planned_assets.append(
            (
                target,
                destination,
                directory,
                _inspect_destination(
                    destination,
                    target,
                    expects_directory=directory,
                ),
            )
        )
    canonical_names = {
        target.name for target, _destination, _directory, _status in planned_assets
    }
    legacy_only = _legacy_only_configuration_paths(home, canonical_names)

    wrapper_directory = home / "bin"
    if wrapper_directory.is_symlink() or (
        wrapper_directory.exists() and not wrapper_directory.is_dir()
    ):
        raise CodexConfigScopeError(
            f"private Codex wrapper directory is unsafe: {wrapper_directory}"
        )
    wrapper = wrapper_directory / "codex"
    if wrapper.is_symlink():
        wrapper_status = "legacy"
    elif wrapper.exists():
        if not wrapper.is_file():
            raise CodexConfigScopeError(
                f"private Codex wrapper path is not a regular file: {wrapper}"
            )
        wrapper_status = "managed" if _is_termdeck_codex_wrapper(wrapper) else "legacy"
    else:
        wrapper_status = "missing"

    return _ConfigurationPlan(
        binding_status=binding_status,
        assets=tuple(planned_assets),
        legacy_only_paths=legacy_only,
        wrapper_path=wrapper,
        wrapper_status=wrapper_status,
    )


def _prepare_private_home(path: str | Path) -> Path:
    requested = Path(path).expanduser()
    try:
        if requested.is_symlink():
            raise CodexConfigScopeError(
                f"private Codex home must not be a symlink: {requested}"
            )
        requested.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = requested.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise CodexConfigScopeError(
                f"private Codex home is not a directory: {requested}"
            )
        requested.chmod(0o700)
        return requested.resolve(strict=True)
    except CodexConfigScopeError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CodexConfigScopeError(
            f"private Codex home cannot be prepared safely: {requested}"
        ) from exc


@contextmanager
def _scope_preparation_lock(home: Path) -> Iterator[None]:
    lock_path = home / ".termdeck-config.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise CodexConfigScopeError(
                f"private Codex configuration lock is unsafe: {lock_path}"
            )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except Exception as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        if isinstance(exc, CodexConfigScopeError):
            raise
        raise CodexConfigScopeError(
            f"private Codex configuration lock cannot be acquired: {lock_path}"
        ) from exc
    assert lock_fd is not None
    try:
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _remove_created_link(destination: Path, target: Path) -> None:
    try:
        if destination.is_symlink() and destination.resolve(
            strict=True
        ) == target.resolve(strict=True):
            destination.unlink()
    except (OSError, RuntimeError):
        # Cleanup must never replace or remove a path which changed after this
        # invocation created it.
        return


def _wrapper_content(private_home: Path, launcher: Path) -> str:
    sqlite_override = f"sqlite_home={json.dumps(str(private_home))}"
    # Keep the private override before all user arguments as a Codex root
    # option. Reject a duplicate sqlite_home override rather than let later CLI
    # flags redirect this scope to shared state (Codex applies duplicate -c
    # values in argument order).
    return "\n".join(
        (
            "#!/bin/sh",
            _WRAPPER_MARKER,
            "set -eu",
            f"export CODEX_HOME={shlex.quote(str(private_home))}",
            f"export CODEX_TERMDECK_GATE_ROOT={shlex.quote(str(private_home / '.termdeck-codex-lifetime'))}",
            "termdeck_expect_config=0",
            'for termdeck_arg in "$@"; do',
            '    if [ "$termdeck_expect_config" -eq 1 ]; then',
            '        case "$termdeck_arg" in',
            "            *sqlite_home*)",
            "                printf '%s\\n' 'codex: sqlite_home is managed by TermDeck for this terminal' >&2",
            "                exit 64",
            "                ;;",
            "        esac",
            "        termdeck_expect_config=0",
            "        continue",
            "    fi",
            '    case "$termdeck_arg" in',
            "        -c|--config)",
            "            termdeck_expect_config=1",
            "            ;;",
            "        --config=*sqlite_home*|-c*sqlite_home*)",
            "            printf '%s\\n' 'codex: sqlite_home is managed by TermDeck for this terminal' >&2",
            "            exit 64",
            "            ;;",
            "    esac",
            "done",
            f"exec {shlex.quote(str(launcher))} "
            f'-c {shlex.quote(sqlite_override)} "$@"',
            "",
        )
    )


def _open_wrapper_directory(path: Path) -> tuple[int, os.stat_result]:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise CodexConfigScopeError(
            f"private Codex wrapper directory cannot be prepared: {path}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CodexConfigScopeError(
            f"private Codex wrapper directory is unsafe: {path}"
        )
    try:
        path.chmod(0o700)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(path, flags)
        opened_metadata = os.fstat(directory_fd)
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex wrapper directory cannot be opened safely: {path}"
        ) from exc
    if not stat.S_ISDIR(opened_metadata.st_mode) or (
        opened_metadata.st_dev,
        opened_metadata.st_ino,
    ) != (metadata.st_dev, metadata.st_ino):
        os.close(directory_fd)
        raise CodexConfigScopeError(
            f"private Codex wrapper directory changed during preparation: {path}"
        )
    return directory_fd, opened_metadata


def _write_wrapper(path: Path, content: str) -> None:
    directory_fd, directory_metadata = _open_wrapper_directory(path.parent)
    try:
        try:
            existing_fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            current = None
        except OSError as exc:
            raise CodexConfigScopeError(
                f"private Codex wrapper cannot be read safely: {path}"
            ) from exc
        else:
            try:
                existing_metadata = os.fstat(existing_fd)
                if not stat.S_ISREG(existing_metadata.st_mode):
                    raise CodexConfigScopeError(
                        f"private Codex wrapper is not a regular file: {path}"
                    )
                with os.fdopen(existing_fd, "r", encoding="utf-8") as handle:
                    existing_fd = -1
                    current = handle.read()
            except (OSError, UnicodeError) as exc:
                raise CodexConfigScopeError(
                    f"private Codex wrapper cannot be read: {path}"
                ) from exc
            finally:
                if existing_fd >= 0:
                    os.close(existing_fd)

        if current == content:
            try:
                os.chmod(
                    path.name,
                    0o700,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise CodexConfigScopeError(
                    f"private Codex wrapper permissions cannot be set: {path}"
                ) from exc
            return

        temporary_name = f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        temporary_created = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            temporary_fd = os.open(
                temporary_name,
                flags,
                0o700,
                dir_fd=directory_fd,
            )
            temporary_created = True
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.chmod(
                path.name,
                0o700,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            current_directory_metadata = path.parent.lstat()
            if not stat.S_ISDIR(current_directory_metadata.st_mode) or (
                current_directory_metadata.st_dev,
                current_directory_metadata.st_ino,
            ) != (directory_metadata.st_dev, directory_metadata.st_ino):
                os.unlink(path.name, dir_fd=directory_fd)
                raise CodexConfigScopeError(
                    f"private Codex wrapper directory changed during publication: {path.parent}"
                )
        except OSError as exc:
            raise CodexConfigScopeError(
                f"private Codex wrapper cannot be written: {path}"
            ) from exc
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(directory_fd)


def prepare_private_bash_launch_command(
    private_home: str | Path,
    *,
    auth_scope_id: str,
    shell_path: str | Path,
) -> str:
    """Build a Bash launch which keeps the tab's private Codex tools first.

    Interactive Bash reads the user's normal ``~/.bashrc`` after tmux applies
    its session environment.  A user rc is allowed to rewrite ``PATH``; on the
    affected host that moved TermDeck's private ``bin/codex`` behind the global
    launcher and silently bypassed the per-tab sqlite override.  This managed
    rc sources the ordinary user file in the *same* final Bash process and then
    reasserts the immutable scope tuple and private-bin precedence.

    Only Bash is supported here.  Callers must reject other legacy shells
    before terminating them rather than discovering an unprovable environment
    after a destructive recreation.
    """

    home = _prepare_private_home(private_home)
    scope_id = str(auth_scope_id).strip()
    if _AUTH_SCOPE_PATTERN.fullmatch(scope_id) is None or home.name != scope_id:
        raise CodexConfigScopeError(
            "private Bash auth scope must match its CODEX_HOME basename"
        )

    requested_shell = Path(shell_path).expanduser()
    if not requested_shell.is_absolute():
        raise CodexConfigScopeError("private Bash launcher must be absolute")
    try:
        shell = requested_shell.resolve(strict=True)
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Bash launcher is unavailable: {requested_shell}"
        ) from exc
    if shell.name.casefold() != "bash" or not shell.is_file() or not os.access(shell, os.X_OK):
        raise CodexConfigScopeError(
            f"private managed-shell migration supports Bash only: {requested_shell}"
        )
    if _is_within(shell, home):
        raise CodexConfigScopeError("private Bash launcher cannot resolve inside CODEX_HOME")

    private_bin = home / "bin"
    gate_root = home / ".termdeck-codex-lifetime"
    rc_path = private_bin / ".termdeck-bashrc"
    content = "\n".join(
        (
            _BASH_RC_MARKER,
            '# Source the user configuration first; TermDeck owns only the final scope tuple.',
            'if [ -n "${HOME:-}" ] && [ -r "$HOME/.bashrc" ]; then',
            '    . "$HOME/.bashrc"',
            "fi",
            f"export CODEX_HOME={shlex.quote(str(home))}",
            f"export TERMDECK_AUTH_SCOPE_ID={shlex.quote(scope_id)}",
            f"export CODEX_TERMDECK_GATE_ROOT={shlex.quote(str(gate_root))}",
            f"export PATH={shlex.quote(str(private_bin))}:\"${{PATH:-/usr/bin:/bin}}\"",
            "",
        )
    )
    _write_wrapper(rc_path, content)
    return shlex.join(
        [
            str(shell),
            "--noprofile",
            "--rcfile",
            str(rc_path),
            "-i",
        ]
    )


def _publish_link(destination: Path, target: Path, *, directory: bool) -> None:
    try:
        destination.symlink_to(target, target_is_directory=directory)
    except FileExistsError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration path changed during publication: {destination}"
        ) from exc
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration link cannot be published: {destination}"
        ) from exc


def _status_from_plan(
    home: Path,
    source: Path,
    launcher: Path | None,
    plan: _ConfigurationPlan,
) -> CodexConfigScopeStatus:
    shared = tuple(
        sorted(
            (
                destination
                for _target, destination, _directory, status in plan.assets
                if status == "shared"
            ),
            key=lambda item: item.name,
        )
    )
    wrapper_path = (
        plan.wrapper_path
        if launcher is not None and plan.wrapper_status == "managed"
        else None
    )
    return CodexConfigScopeStatus(
        private_home=home,
        canonical_home=source,
        shared_paths=shared,
        migration_required_paths=tuple(
            sorted(
                plan.migration_required_paths,
                key=lambda item: str(item.relative_to(home)),
            )
        ),
        launcher_path=launcher,
        wrapper_path=wrapper_path,
    )


def _new_migration_backup(home: Path) -> Path:
    root = home / _CONFIG_BACKUP_ROOT
    try:
        if root.is_symlink():
            raise CodexConfigScopeError(
                f"private Codex configuration backup root is unsafe: {root}"
            )
        root.mkdir(mode=0o700, exist_ok=True)
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise CodexConfigScopeError(
                f"private Codex configuration backup root is unsafe: {root}"
            )
        root.chmod(0o700)
        backup = root / f"migration-{uuid.uuid4().hex}"
        backup.mkdir(mode=0o700)
        return backup
    except CodexConfigScopeError:
        raise
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration backup cannot be created: {root}"
        ) from exc


def _backup_destination(backup: Path, home: Path, original: Path) -> Path:
    try:
        relative = original.relative_to(home)
    except ValueError as exc:  # pragma: no cover - plans contain only home children.
        raise CodexConfigScopeError(
            f"private Codex migration path is outside its scope: {original}"
        ) from exc
    destination = backup / relative
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = destination.parent
        while current != backup.parent:
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise CodexConfigScopeError(
                    f"private Codex configuration backup path is unsafe: {current}"
                )
            current.chmod(0o700)
            if current == backup:
                break
            current = current.parent
    except CodexConfigScopeError:
        raise
    except OSError as exc:
        raise CodexConfigScopeError(
            f"private Codex configuration backup path cannot be prepared: {destination}"
        ) from exc
    return destination


def _remove_empty_backup(backup: Path) -> None:
    try:
        directories = sorted(
            (path for path in backup.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            directory.rmdir()
        backup.rmdir()
        backup.parent.rmdir()
    except OSError:
        # A non-empty backup is evidence which must be preserved. Cleanup is
        # cosmetic and must never hide a migration or rollback failure.
        return


def _restore_wrapper_state(path: Path, previous: str | None) -> None:
    if previous is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not _is_termdeck_codex_wrapper(path):
                raise CodexConfigScopeError(
                    f"private Codex wrapper changed during rollback: {path}"
                )
            path.unlink()
        return
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except (OSError, UnicodeError) as exc:
        raise CodexConfigScopeError(
            f"private Codex wrapper cannot be inspected during rollback: {path}"
        ) from exc
    if current != previous:
        _write_wrapper(path, previous)


def _rollback_migration(
    *,
    plan: _ConfigurationPlan,
    moved: list[tuple[Path, Path]],
    created_links: list[tuple[Path, Path]],
    previous_wrapper: str | None,
) -> list[str]:
    errors: list[str] = []
    for destination, target in reversed(created_links):
        try:
            if not destination.is_symlink() or destination.resolve(
                strict=True
            ) != target.resolve(strict=True):
                raise CodexConfigScopeError(
                    f"published link changed before rollback: {destination}"
                )
            destination.unlink()
        except (OSError, RuntimeError, CodexConfigScopeError) as exc:
            errors.append(str(exc))
    try:
        _restore_wrapper_state(plan.wrapper_path, previous_wrapper)
    except (OSError, RuntimeError, CodexConfigScopeError) as exc:
        errors.append(str(exc))
    for original, saved in reversed(moved):
        try:
            if original.exists() or original.is_symlink():
                raise CodexConfigScopeError(
                    f"private Codex migration path changed before rollback: {original}"
                )
            os.replace(saved, original)
        except (OSError, RuntimeError, CodexConfigScopeError) as exc:
            errors.append(str(exc))
    return errors


def prepare_shared_codex_configuration(
    private_home: str | Path,
    canonical_home: str | Path,
    *,
    launcher_attestor: LauncherAttestor | None = None,
) -> CodexConfigScopeStatus:
    """Share canonical Codex configuration while isolating mutable state.

    Missing configuration assets become direct links to the canonical home.
    Existing regular files/directories are preserved and explicitly reported
    as requiring a stopped-scope migration.  Authentication selection, SQLite
    state, conversation storage, and other runtime paths are outside this
    helper's ownership.

    A private ``bin/codex`` wrapper injects ``sqlite_home`` as a root ``-c``
    override before all user arguments.  That session-flags layer outranks a
    canonical or profile config's ``sqlite_home`` while every other canonical
    setting remains shared.
    """

    requested_home = Path(private_home).expanduser()
    if requested_home.is_symlink():
        raise CodexConfigScopeError(
            f"private Codex home must not be a symlink: {requested_home}"
        )
    try:
        requested_resolved = requested_home.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CodexConfigScopeError(
            f"private Codex home cannot be resolved: {requested_home}"
        ) from exc
    requested_source = Path(canonical_home).expanduser()
    source_is_symlink = requested_source.is_symlink()
    try:
        source = requested_source.resolve(strict=True)
    except FileNotFoundError:
        if source_is_symlink:
            raise CodexConfigScopeError(
                f"canonical Codex home is a broken symlink: {requested_source}"
            )
        try:
            unavailable_source = requested_source.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise CodexConfigScopeError(
                f"canonical Codex home cannot be resolved: {canonical_home}"
            ) from exc
        return CodexConfigScopeStatus(
            private_home=requested_resolved,
            canonical_home=unavailable_source,
            shared_paths=(),
            migration_required_paths=(),
            launcher_path=None,
            wrapper_path=None,
            canonical_available=False,
        )
    except (OSError, RuntimeError) as exc:
        raise CodexConfigScopeError(
            f"canonical Codex home cannot be resolved: {canonical_home}"
        ) from exc
    if not source.is_dir():
        return CodexConfigScopeStatus(
            private_home=requested_resolved,
            canonical_home=source,
            shared_paths=(),
            migration_required_paths=(),
            launcher_path=None,
            wrapper_path=None,
            canonical_available=False,
        )

    if source == requested_resolved:
        requested_home_absolute = Path(os.path.abspath(requested_home))
        requested_source_absolute = Path(os.path.abspath(requested_source))
        if requested_source_absolute != requested_home_absolute:
            raise CodexConfigScopeError(
                f"canonical Codex home aliases the private home: {requested_source}"
            )
        return CodexConfigScopeStatus(
            private_home=requested_resolved,
            canonical_home=source,
            shared_paths=(),
            migration_required_paths=(),
            launcher_path=None,
            wrapper_path=None,
            canonical_home_in_use=True,
        )
    if requested_resolved in source.parents:
        raise CodexConfigScopeError(
            f"canonical Codex home is inside the private home: {requested_source}"
        )
    _validate_canonical_root(source)

    home = requested_resolved

    # Resolve before publishing any links so an invalid explicit launcher does
    # not leave a half-prepared private home.
    launcher = resolve_codex_launcher(
        home,
        launcher_attestor=launcher_attestor,
    )
    prepared_home = _prepare_private_home(requested_home)
    if prepared_home != home:
        raise CodexConfigScopeError(
            f"private Codex home changed during preparation: {requested_home}"
        )

    with _scope_preparation_lock(home):
        plan = _configuration_plan(home, source)
        created_links: list[tuple[Path, Path]] = []
        try:
            if plan.binding_status == "missing":
                binding = home / _CANONICAL_HOME_BINDING
                _publish_link(binding, source, directory=True)
                created_links.append((binding, source))
            for target, destination, directory, status in plan.assets:
                if status != "missing":
                    continue
                _publish_link(destination, target, directory=directory)
                created_links.append((destination, target))
            if plan.wrapper_status != "legacy" and launcher is not None:
                _write_wrapper(
                    plan.wrapper_path,
                    _wrapper_content(home, launcher),
                )
        except Exception:
            for destination, target in reversed(created_links):
                _remove_created_link(destination, target)
            raise
        final_plan = _configuration_plan(home, source)

    return _status_from_plan(home, source, launcher, final_plan)


def migrate_quiescent_codex_configuration(
    private_home: str | Path,
    *,
    launcher_attestor: LauncherAttestor | None = None,
) -> CodexConfigMigrationResult | None:
    """Replace legacy private configuration after the scope is proven stopped.

    This function does not inspect processes.  Its caller must hold the
    scope's lifecycle/auth transaction lock and must have an exact receipt
    proving that no tmux session or Codex process can still use ``private_home``.

    The canonical source is taken only from the binding persisted by
    :func:`prepare_shared_codex_configuration`.  Legacy paths are revalidated
    under the per-scope configuration lock, moved into one unique private
    backup, and replaced with canonical links plus the managed launcher.  A
    caught failure restores every moved path; successful backups are retained.
    """

    home = _prepare_private_home(private_home)
    binding = home / _CANONICAL_HOME_BINDING
    if not binding.exists() and not binding.is_symlink():
        # Anonymous scopes have no canonical configuration authority and no
        # migration to perform. This mirrors preparation's explicit no-op.
        return None

    with _scope_preparation_lock(home):
        source = _bound_canonical_home(home)
        plan = _configuration_plan(home, source)
        launcher = resolve_codex_launcher(
            home,
            launcher_attestor=launcher_attestor,
        )
        if launcher is None:
            raise CodexConfigScopeError(
                "no safe host Codex launcher is available for private-scope migration"
            )

        migration_paths = plan.migration_required_paths
        if not migration_paths:
            _write_wrapper(
                plan.wrapper_path,
                _wrapper_content(home, launcher),
            )
            final_plan = _configuration_plan(home, source)
            return CodexConfigMigrationResult(
                status=_status_from_plan(home, source, launcher, final_plan),
                backup_path=None,
                migrated_paths=(),
            )

        previous_wrapper = (
            plan.wrapper_path.read_text(encoding="utf-8")
            if plan.wrapper_status == "managed"
            else None
        )
        backup = _new_migration_backup(home)
        moved: list[tuple[Path, Path]] = []
        created_links: list[tuple[Path, Path]] = []
        try:
            for original in migration_paths:
                saved = _backup_destination(backup, home, original)
                try:
                    os.replace(original, saved)
                except OSError as exc:
                    raise CodexConfigScopeError(
                        f"legacy private Codex configuration cannot be backed up: {original}"
                    ) from exc
                moved.append((original, saved))

            publication_plan = _configuration_plan(home, source)
            if publication_plan.migration_required_paths:
                raise CodexConfigScopeError(
                    "private Codex configuration changed during stopped-scope migration"
                )
            for (
                target,
                destination,
                directory,
                asset_status,
            ) in publication_plan.assets:
                if asset_status != "missing":
                    continue
                _publish_link(destination, target, directory=directory)
                created_links.append((destination, target))
            _write_wrapper(
                publication_plan.wrapper_path,
                _wrapper_content(home, launcher),
            )
            final_plan = _configuration_plan(home, source)
            status = _status_from_plan(home, source, launcher, final_plan)
            if not status.ready:
                raise CodexConfigScopeError(
                    "private Codex configuration migration did not reach canonical parity"
                )
        except Exception as exc:
            rollback_errors = _rollback_migration(
                plan=plan,
                moved=moved,
                created_links=created_links,
                previous_wrapper=previous_wrapper,
            )
            if not rollback_errors:
                _remove_empty_backup(backup)
            if rollback_errors:
                raise CodexConfigScopeError(
                    "private Codex configuration migration failed and rollback was "
                    f"incomplete: {'; '.join(rollback_errors)}"
                ) from exc
            if isinstance(exc, CodexConfigScopeError):
                raise
            raise CodexConfigScopeError(
                "private Codex configuration migration failed"
            ) from exc

        return CodexConfigMigrationResult(
            status=status,
            backup_path=backup,
            migrated_paths=migration_paths,
        )
