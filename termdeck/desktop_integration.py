from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shlex
import stat
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the guarded operations fail closed.
    fcntl = None

APP_ID = "io.github.t1000.T1000"
DESKTOP_FILENAME = f"{APP_ID}.desktop"
AUTOSTART_FILENAME = DESKTOP_FILENAME
ICON_NAME = "t1000"
MANAGED_LAUNCHER = Path("/warp/projects/softwares/termdeck/.termdeck/bin/t1000")
MANAGED_ROOT_ENV = "T1000_MANAGED_ROOT"
DEFAULT_MANAGED_ROOT = Path(
    "/warp/projects/softwares/termdeck/.termdeck/t1000-managed"
)
AUTOSTART_RESOURCE = "io.github.t1000.T1000.autostart.desktop"
MAX_DESKTOP_TARGET_BYTES = 1024 * 1024
DESKTOP_DATABASE_TIMEOUT_SECONDS = 15.0
DESKTOP_TARGET_LOCK_TIMEOUT_SECONDS = 5.0
DESKTOP_TARGET_LOCK_POLL_SECONDS = 0.05

TERMDECK_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#1e1e1e"/>
  <rect x="14" y="18" width="100" height="92" rx="10" fill="#101010" stroke="#5f6368" stroke-width="4"/>
  <rect x="14" y="18" width="100" height="20" rx="10" fill="#2b2b2b"/>
  <circle cx="29" cy="28" r="4" fill="#ef4444"/>
  <circle cx="43" cy="28" r="4" fill="#f59e0b"/>
  <circle cx="57" cy="28" r="4" fill="#22c55e"/>
  <path d="M31 58l16 14-16 14" fill="none" stroke="#8ae234" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M58 88h34" fill="none" stroke="#d3d7cf" stroke-width="8" stroke-linecap="round"/>
</svg>
"""


@dataclass(frozen=True)
class DesktopPaths:
    data_home: Path
    config_home: Path
    applications_dir: Path
    icon_dir: Path
    desktop_file: Path
    icon_file: Path
    autostart_dir: Path
    autostart_file: Path


@dataclass(frozen=True)
class DesktopInstallPlan:
    command: str
    paths: DesktopPaths
    desktop_entry: str
    autostart_entry: str
    icon_svg: str = TERMDECK_ICON_SVG

    def as_dict(self) -> dict[str, object]:
        return {
            "app_id": APP_ID,
            "command": self.command,
            "desktop_file": str(self.paths.desktop_file),
            "icon_file": str(self.paths.icon_file),
            "applications_dir": str(self.paths.applications_dir),
            "icon_dir": str(self.paths.icon_dir),
            "desktop_entry": self.desktop_entry,
            "autostart_dir": str(self.paths.autostart_dir),
            "autostart_file": str(self.paths.autostart_file),
            "autostart_entry": self.autostart_entry,
            "managed_launcher": str(MANAGED_LAUNCHER),
        }


class DesktopIntegrationError(OSError):
    """A desktop integration target failed its safety or durability contract."""


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    content: bytes = b""
    mode: int = 0o644
    device: int = 0
    inode: int = 0
    size: int = 0
    modified_ns: int = 0


@dataclass
class _PublicationTarget:
    path: Path
    content: bytes
    mode: int
    label: str
    parent_descriptor: int
    parent_metadata: os.stat_result
    lock_descriptor: int = -1
    preimage: _FileSnapshot | None = None
    published: _FileSnapshot | None = None


class _PublishedWriteError(DesktopIntegrationError):
    def __init__(self, message: str, published: _FileSnapshot) -> None:
        super().__init__(message)
        self.published = published


class _DesktopDatabaseUpdateError(DesktopIntegrationError):
    def __init__(self, message: str, status: dict[str, object]) -> None:
        super().__init__(message)
        self.status = status


def _absolute_user_path(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DesktopIntegrationError(f"{label} must be an absolute path")
    return path


def xdg_data_home(env: dict[str, str] | None = None) -> Path:
    active_environment = os.environ if env is None else env
    raw = active_environment.get("XDG_DATA_HOME")
    if raw:
        if not Path(raw).is_absolute():
            raise DesktopIntegrationError("XDG_DATA_HOME must be an absolute path")
        return _absolute_user_path(raw, label="XDG_DATA_HOME")
    return Path.home() / ".local" / "share"


def xdg_config_home(env: dict[str, str] | None = None) -> Path:
    active_environment = os.environ if env is None else env
    raw = active_environment.get("XDG_CONFIG_HOME")
    if raw:
        if not Path(raw).is_absolute():
            raise DesktopIntegrationError("XDG_CONFIG_HOME must be an absolute path")
        return _absolute_user_path(raw, label="XDG_CONFIG_HOME")
    return Path.home() / ".config"


def desktop_paths(
    data_home: str | Path | None = None,
    config_home: str | Path | None = None,
) -> DesktopPaths:
    root = (
        _absolute_user_path(data_home, label="desktop data home")
        if data_home is not None
        else xdg_data_home()
    )
    config_root = (
        _absolute_user_path(config_home, label="desktop config home")
        if config_home is not None
        else xdg_config_home()
    )
    applications_dir = root / "applications"
    icon_dir = root / "icons" / "hicolor" / "scalable" / "apps"
    autostart_dir = config_root / "autostart"
    return DesktopPaths(
        data_home=root,
        config_home=config_root,
        applications_dir=applications_dir,
        icon_dir=icon_dir,
        desktop_file=applications_dir / DESKTOP_FILENAME,
        icon_file=icon_dir / f"{ICON_NAME}.svg",
        autostart_dir=autostart_dir,
        autostart_file=autostart_dir / AUTOSTART_FILENAME,
    )


def resolve_launch_command(command: str | None = None) -> str:
    """Return a desktop Exec command that does not require users to know Python.

    Prefer an installed `termdeck` entry point. When running straight from a
    source tree, fall back to the current interpreter with `-m termdeck`.
    """
    if command and command.strip():
        base = command.strip()
    else:
        found = shutil.which("t1000")
        base = shlex.quote(found) if found else shlex.join([sys.executable, "-m", "termdeck"])
    if " gui" in base or base.endswith(" gui"):
        return base
    return f"{base} gui"


def build_desktop_entry(exec_command: str) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=t1000",
        "GenericName=Terminal Task Manager",
        "Comment=Named terminal windows with directory-backed tabs",
        f"Exec={exec_command}",
        f"Icon={ICON_NAME}",
        "Terminal=false",
        "StartupNotify=true",
        "Categories=System;TerminalEmulator;Development;Utility;",
        "Keywords=terminal;tmux;shell;directories;windows;tabs;",
    ]
    return "\n".join(lines) + "\n"


def _command_launcher(exec_command: str) -> Path:
    reserved = set("\"'\\`<>~|&;$*?#(){}[]%")
    if any(
        character in "\x00\r\n\t" or character in reserved
        for character in exec_command
    ):
        raise DesktopIntegrationError(
            "desktop command contains syntax requiring unsupported Desktop Entry quoting"
        )
    try:
        fields = shlex.split(exec_command)
    except ValueError as exc:
        raise DesktopIntegrationError(
            f"desktop command is not valid shell-free Exec syntax: {exc}"
        ) from exc
    if not fields or not fields[0]:
        raise DesktopIntegrationError("desktop command has no executable")
    executable = Path(fields[0]).expanduser()
    if not executable.is_absolute():
        found = shutil.which(fields[0])
        if found:
            executable = Path(found)
    return executable


def _build_autostart_entry_for_launcher(launcher: Path) -> str:
    """Return the managed GUI autostart contract.

    The entry deliberately contains no shell, environment assignment, or
    alternate executable.  Desktop-session launchers pass through the current
    graphical DBus/display environment, while the managed T1000 launcher is
    responsible for release and SSD-root verification.
    """

    resolved_command = f"{launcher} gui"
    resource = resources.files("termdeck").joinpath("data", AUTOSTART_RESOURCE)
    template = resource.read_text(encoding="utf-8")
    markers = (
        "@T1000_EXEC@",
        "@T1000_TRYEXEC@",
        "@T1000_MANAGED_LAUNCHER@",
    )
    if any(marker not in template for marker in markers):
        raise DesktopIntegrationError("autostart template is missing its command markers")
    rendered = (
        template.replace("@T1000_EXEC@", resolved_command)
        .replace("@T1000_TRYEXEC@", shlex.quote(str(launcher)))
        .replace("@T1000_MANAGED_LAUNCHER@", str(launcher))
    )
    if any(marker in rendered for marker in markers):
        raise DesktopIntegrationError("autostart template retained a command marker")
    expected_values = {
        "Exec": resolved_command,
        "TryExec": str(launcher),
        "X-T1000-Managed-Launcher": str(launcher),
    }
    observed: dict[str, list[str]] = {key: [] for key in expected_values}
    for line in rendered.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in observed:
            observed[key].append(value)
    if any(observed[key] != [value] for key, value in expected_values.items()):
        raise DesktopIntegrationError(
            "autostart template has duplicate or conflicting launcher keys"
        )
    return rendered


def build_autostart_entry() -> str:
    return _build_autostart_entry_for_launcher(MANAGED_LAUNCHER)


def _managed_root() -> Path:
    raw = os.environ.get(MANAGED_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_MANAGED_ROOT


def _release_manager_owns_autostart(path: Path) -> bool:
    return _absolute_path(path) == _absolute_path(desktop_paths().autostart_file)


def _non_rotational_device(path: Path) -> bool:
    try:
        device = path.stat().st_dev
        rotational = Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}" / "queue" / "rotational"
        return rotational.read_text(encoding="ascii").strip() == "0"
    except (OSError, UnicodeError, ValueError):
        return False


def _managed_path_metadata_error(
    path: Path,
    metadata: os.stat_result,
    *,
    directory: bool,
) -> str | None:
    if metadata.st_uid != os.geteuid():
        return f"managed path is not owned by the current user: {path}"
    if stat.S_IMODE(metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        return f"managed path is group/world writable: {path}"
    if directory:
        if metadata.st_nlink < 2:
            return f"managed directory has an unsafe link count: {path}"
    elif metadata.st_nlink != 1:
        return f"managed file has multiple hard links: {path}"
    return None


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_nofollow() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise DesktopIntegrationError(
            "desktop integration requires O_NOFOLLOW and cannot run safely on this platform"
        )
    if not isinstance(directory, int) or directory == 0:
        raise DesktopIntegrationError(
            "desktop integration requires O_DIRECTORY and cannot run safely on this platform"
        )
    return nofollow


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | _require_nofollow()
    )


def _rename_noreplace_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    for name in (source_name, destination_name):
        if not name or name in {".", ".."} or os.sep in name:
            raise DesktopIntegrationError(
                "desktop integration rename has an unsafe path component"
            )
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise DesktopIntegrationError(
            "desktop integration requires renameat2(RENAME_NOREPLACE) for safe rollback"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.ENOENT:
        raise FileNotFoundError(error, os.strerror(error), source_name)
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination_name)
    if error in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.ENOTSUP,
    }:
        raise DesktopIntegrationError(
            "desktop integration requires renameat2(RENAME_NOREPLACE) for safe rollback"
        )
    raise OSError(error, os.strerror(error), source_name, destination_name)


def _validate_directory_component(path: Path, metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise DesktopIntegrationError(
            f"desktop integration parent is not a real directory: {path}"
        )


def _open_directory_chain(path: Path, *, create: bool) -> int | None:
    """Open an absolute directory one component at a time without links."""

    directory = _absolute_path(path)
    flags = _directory_open_flags()
    try:
        descriptor = os.open(directory.anchor or os.sep, flags)
    except OSError as exc:
        raise DesktopIntegrationError(
            f"could not open desktop integration root safely: {directory}: {exc}"
        ) from exc
    try:
        current_path = Path(directory.anchor or os.sep)
        _validate_directory_component(current_path, os.fstat(descriptor))
        for component in directory.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise DesktopIntegrationError(
                        f"could not create desktop integration directory safely: {directory}: {exc}"
                    ) from exc
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise DesktopIntegrationError(
                        f"could not open created desktop integration directory safely: {directory}: {exc}"
                    ) from exc
            except OSError as exc:
                raise DesktopIntegrationError(
                    f"could not open desktop integration parent safely: {directory}: {exc}"
                ) from exc
            try:
                child_metadata = os.fstat(child)
                current_path /= component
                _validate_directory_component(current_path, child_metadata)
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> tuple[int, os.stat_result]:
    if not name or name in {".", ".."} or os.sep in name:
        raise DesktopIntegrationError(f"{label} has an unsafe path component")
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise DesktopIntegrationError(f"could not open {label} safely: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise DesktopIntegrationError(f"{label} is not a directory")
    return descriptor, metadata


def _open_regular_file_at(
    parent_descriptor: int,
    name: str,
    *,
    path: Path,
    label: str,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _require_nofollow()
    descriptor = -1
    try:
        observed = os.lstat(name, dir_fd=parent_descriptor)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise DesktopIntegrationError(f"could not open {label} safely: {path}: {exc}") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or observed.st_dev != metadata.st_dev
        or observed.st_ino != metadata.st_ino
    ):
        os.close(descriptor)
        raise DesktopIntegrationError(f"{label} is not a stable regular file: {path}")
    return descriptor, metadata


def _read_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int | None = None,
    label: str = "file",
) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if maximum_bytes is not None and total > maximum_bytes:
            raise DesktopIntegrationError(f"{label} exceeds the inspection limit")
        chunks.append(chunk)


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _revalidate_directory_path(path: Path, expected: os.stat_result) -> None:
    descriptor = _open_directory_chain(path, create=False)
    if descriptor is None:
        raise DesktopIntegrationError(f"managed directory disappeared: {path}")
    try:
        if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(expected):
            raise DesktopIntegrationError(f"managed directory identity changed: {path}")
    finally:
        os.close(descriptor)


def _revalidate_regular_path(
    path: Path,
    expected: os.stat_result,
    expected_content: bytes,
    *,
    label: str,
) -> None:
    parent_descriptor = _open_directory_chain(path.parent, create=False)
    if parent_descriptor is None:
        raise DesktopIntegrationError(f"{label} parent disappeared: {path.parent}")
    descriptor = -1
    try:
        descriptor, metadata = _open_regular_file_at(
            parent_descriptor,
            path.name,
            path=path,
            label=label,
        )
        if _metadata_signature(metadata) != _metadata_signature(expected):
            raise DesktopIntegrationError(f"{label} identity changed: {path}")
        if _read_descriptor(descriptor, maximum_bytes=65536) != expected_content:
            raise DesktopIntegrationError(f"{label} content changed: {path}")
        if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(expected):
            raise DesktopIntegrationError(f"{label} metadata changed: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _managed_launcher_interpreter(content: bytes, bootstrap: Path) -> Path:
    if b"\r" in content or not content.endswith(b"\n") or content.count(b"\n") != 3:
        raise DesktopIntegrationError(
            "managed launcher does not use canonical LF-only release launcher bytes"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise DesktopIntegrationError("managed launcher is not valid UTF-8") from exc
    lines = text[:-1].split("\n")
    if len(lines) != 3 or lines[:2] != ["#!/bin/sh", "set -eu"]:
        raise DesktopIntegrationError("managed launcher does not use the exact release launcher contract")
    try:
        fields = shlex.split(lines[2], posix=True)
    except ValueError as exc:
        raise DesktopIntegrationError("managed launcher command is invalid") from exc
    if (
        len(fields) != 6
        or fields[0] != "exec"
        or fields[2:4] != ["-I", "-B"]
        or fields[4] != str(bootstrap)
        or fields[5] != "$@"
    ):
        raise DesktopIntegrationError("managed launcher does not target the selected SSD bootstrap exactly")
    interpreter = Path(fields[1])
    if not interpreter.is_absolute():
        raise DesktopIntegrationError("managed launcher interpreter is not absolute")
    expected_command = (
        f'exec {shlex.quote(str(interpreter))} -I -B '
        f'{shlex.quote(str(bootstrap))} "$@"'
    )
    if lines[2] != expected_command:
        raise DesktopIntegrationError("managed launcher command syntax is not canonical")
    expected_content = f"#!/bin/sh\nset -eu\n{expected_command}\n".encode("utf-8")
    if content != expected_content:
        raise DesktopIntegrationError(
            "managed launcher bytes do not match the canonical release launcher"
        )
    return interpreter


def managed_autostart_ready(
    *,
    launcher: str | Path = MANAGED_LAUNCHER,
    managed_root: str | Path | None = None,
) -> tuple[bool, str]:
    """Verify the autostart target is a mounted, non-rotational release root.

    This check is intentionally conservative and is used only before writing
    the autostart file.  The small launcher may remain on the normal home
    filesystem; the executable bootstrap and release root must be on the SSD.
    At session startup, ``TryExec`` plus the managed bootstrap's immutable-
    release checks fail closed if the mount disappears.
    """

    launcher_path = _absolute_path(Path(launcher).expanduser())
    root = (
        _absolute_path(Path(managed_root).expanduser())
        if managed_root is not None
        else _absolute_path(_managed_root())
    )
    storage_root = root.parent
    bootstrap = root / "bootstrap.py"
    state_path = root / "state.json"
    launcher_parent_descriptor = -1
    launcher_descriptor = -1
    storage_descriptor = -1
    root_descriptor = -1
    bootstrap_descriptor = -1
    state_descriptor = -1
    try:
        _require_nofollow()
        opened_launcher_parent = _open_directory_chain(
            launcher_path.parent,
            create=False,
        )
        launcher_parent_descriptor = (
            opened_launcher_parent if opened_launcher_parent is not None else -1
        )
        if launcher_parent_descriptor < 0:
            raise DesktopIntegrationError("managed launcher parent is unavailable")
        launcher_descriptor, launcher_stat = _open_regular_file_at(
            launcher_parent_descriptor,
            launcher_path.name,
            path=launcher_path,
            label="managed launcher",
        )
        opened_storage = _open_directory_chain(storage_root, create=False)
        storage_descriptor = opened_storage if opened_storage is not None else -1
        if storage_descriptor < 0:
            raise DesktopIntegrationError("managed storage root is unavailable")
        storage_stat = os.fstat(storage_descriptor)
        root_descriptor, root_stat = _open_child_directory(
            storage_descriptor,
            root.name,
            label="managed release root",
        )
        bootstrap_descriptor, bootstrap_stat = _open_regular_file_at(
            root_descriptor,
            bootstrap.name,
            path=bootstrap,
            label="managed bootstrap",
        )
        state_descriptor, state_stat = _open_regular_file_at(
            root_descriptor,
            state_path.name,
            path=state_path,
            label="managed release state",
        )
        for path, metadata, directory in (
            (launcher_path, launcher_stat, False),
            (root, root_stat, True),
            (storage_root, storage_stat, True),
            (bootstrap, bootstrap_stat, False),
            (state_path, state_stat, False),
        ):
            metadata_error = _managed_path_metadata_error(
                path,
                metadata,
                directory=directory,
            )
            if metadata_error:
                raise DesktopIntegrationError(metadata_error)
        if stat.S_IMODE(launcher_stat.st_mode) & stat.S_IXUSR == 0:
            raise DesktopIntegrationError("managed launcher is not owner-executable")
        if stat.S_IMODE(bootstrap_stat.st_mode) & stat.S_IXUSR == 0:
            raise DesktopIntegrationError("managed bootstrap is not owner-executable")
        if (
            root_stat.st_dev != storage_stat.st_dev
            or bootstrap_stat.st_dev != storage_stat.st_dev
        ):
            raise DesktopIntegrationError(
                "managed release root and bootstrap are not on one storage device"
            )
        launcher_content = _read_descriptor(
            launcher_descriptor,
            maximum_bytes=65536,
            label="managed launcher",
        )
        bootstrap_content = _read_descriptor(
            bootstrap_descriptor,
            maximum_bytes=65536,
            label="managed bootstrap",
        )
        state_content = _read_descriptor(
            state_descriptor,
            maximum_bytes=65536,
            label="managed release state",
        )
        interpreter = _managed_launcher_interpreter(launcher_content, bootstrap)
        try:
            state = json.loads(state_content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopIntegrationError("managed release state is not valid JSON") from exc
        if not isinstance(state, dict):
            raise DesktopIntegrationError("managed release state is not an object")
        expected_state = {
            "launcher_path": str(launcher_path),
            "python_executable": str(interpreter),
            "launcher_sha256": hashlib.sha256(launcher_content).hexdigest(),
            "bootstrap_sha256": hashlib.sha256(bootstrap_content).hexdigest(),
        }
        for field, expected in expected_state.items():
            if state.get(field) != expected:
                raise DesktopIntegrationError(
                    f"managed release state {field} does not match the pinned activation"
                )
        if _metadata_signature(os.fstat(launcher_descriptor)) != _metadata_signature(launcher_stat):
            raise DesktopIntegrationError("managed launcher changed while being inspected")

        from .storage_fabric import attest_storage_fabric

        attestation = attest_storage_fabric(
            managed_root=root,
            launcher_path=launcher_path,
            production=True,
        )
        if not attestation.ok:
            raise DesktopIntegrationError(attestation.reason)
        for descriptor, expected, label in (
            (launcher_descriptor, launcher_stat, "managed launcher"),
            (storage_descriptor, storage_stat, "managed storage root"),
            (root_descriptor, root_stat, "managed release root"),
            (bootstrap_descriptor, bootstrap_stat, "managed bootstrap"),
            (state_descriptor, state_stat, "managed release state"),
        ):
            if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(expected):
                raise DesktopIntegrationError(f"{label} metadata changed during attestation")
        if _read_descriptor(
            launcher_descriptor,
            maximum_bytes=65536,
            label="managed launcher",
        ) != launcher_content:
            raise DesktopIntegrationError("managed launcher content changed during attestation")
        if _read_descriptor(
            state_descriptor,
            maximum_bytes=65536,
            label="managed release state",
        ) != state_content:
            raise DesktopIntegrationError("managed release state changed during attestation")
        _revalidate_regular_path(
            launcher_path,
            launcher_stat,
            launcher_content,
            label="managed launcher",
        )
        _revalidate_directory_path(storage_root, storage_stat)
        _revalidate_directory_path(root, root_stat)
        _revalidate_regular_path(
            bootstrap,
            bootstrap_stat,
            bootstrap_content,
            label="managed bootstrap",
        )
        _revalidate_regular_path(
            state_path,
            state_stat,
            state_content,
            label="managed release state",
        )
        return True, attestation.reason
    except Exception as exc:
        return False, f"managed autostart verification failed closed: {exc}"
    finally:
        for descriptor in (
            state_descriptor,
            bootstrap_descriptor,
            root_descriptor,
            storage_descriptor,
            launcher_descriptor,
            launcher_parent_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _ensure_real_directory(path: Path) -> Path:
    directory = _absolute_path(path)
    descriptor = _open_directory_chain(directory, create=True)
    if descriptor is None:  # pragma: no cover - create=True cannot return None.
        raise DesktopIntegrationError(
            f"could not create desktop integration directory safely: {directory}"
        )
    os.close(descriptor)
    return directory


def _open_real_directory(path: Path) -> int:
    directory = _absolute_path(path)
    descriptor = _open_directory_chain(directory, create=False)
    if descriptor is None:
        raise DesktopIntegrationError(
            f"could not open desktop integration directory safely: {directory}: missing"
        )
    return descriptor


def _open_existing_directory_chain(path: Path) -> int | None:
    """Open an existing directory chain without following any ancestor link.

    The descriptor is walked from the filesystem root so a parent replacement
    after validation cannot redirect a later operation to a different tree.
    A missing component returns ``None`` for idempotent removal callers.
    """

    return _open_directory_chain(path, create=False)


def _fsync_directory(path: Path) -> None:
    descriptor = _open_real_directory(path)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not fsync desktop integration directory: {path}: {exc}"
            ) from exc
    finally:
        os.close(descriptor)


def _destination_stat(descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DesktopIntegrationError(
            f"could not inspect desktop integration target {name}: {exc}"
        ) from exc


def _validate_destination_stat(
    path: Path,
    observed: os.stat_result | None,
    expected: _FileSnapshot | None = None,
) -> None:
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise DesktopIntegrationError(
                f"desktop integration destination is not a regular file: {path}"
            )
        if observed.st_nlink != 1:
            raise DesktopIntegrationError(
                f"desktop integration destination has multiple hard links: {path}"
            )
        if observed.st_uid != os.geteuid():
            raise DesktopIntegrationError(
                f"desktop integration destination is not owned by the current user: {path}"
            )
        if stat.S_IMODE(observed.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise DesktopIntegrationError(
                f"desktop integration destination is group/world writable: {path}"
            )
    if expected is None:
        return
    if expected.exists:
        if (
            observed is None
            or observed.st_dev != expected.device
            or observed.st_ino != expected.inode
            or observed.st_size != expected.size
            or observed.st_mtime_ns != expected.modified_ns
        ):
            raise DesktopIntegrationError(
                f"desktop integration destination changed before publication: {path}"
            )
    elif observed is not None:
        raise DesktopIntegrationError(
            f"desktop integration destination appeared before publication: {path}"
        )


def _snapshot_file_at(
    descriptor: int,
    target: Path,
    *,
    label: str,
) -> _FileSnapshot:
    observed = _destination_stat(descriptor, target.name)
    _validate_destination_stat(target, observed)
    if observed is None:
        return _FileSnapshot(False)
    if observed.st_size > MAX_DESKTOP_TARGET_BYTES:
        raise DesktopIntegrationError(
            f"existing {label} exceeds the inspection limit: {target}"
        )
    file_descriptor, opened = _open_regular_file_at(
        descriptor,
        target.name,
        path=target,
        label=f"existing {label}",
    )
    try:
        content = _read_descriptor(
            file_descriptor,
            maximum_bytes=MAX_DESKTOP_TARGET_BYTES,
            label=f"existing {label}",
        )
        final_descriptor = os.fstat(file_descriptor)
        final_name = _destination_stat(descriptor, target.name)
        if (
            final_name is None
            or _metadata_signature(final_descriptor) != _metadata_signature(opened)
            or final_name.st_dev != opened.st_dev
            or final_name.st_ino != opened.st_ino
            or final_name.st_size != opened.st_size
            or final_name.st_mtime_ns != opened.st_mtime_ns
        ):
            raise DesktopIntegrationError(
                f"existing {label} changed while being read: {target}"
            )
        return _FileSnapshot(
            True,
            content,
            stat.S_IMODE(final_descriptor.st_mode),
            final_descriptor.st_dev,
            final_descriptor.st_ino,
            final_descriptor.st_size,
            final_descriptor.st_mtime_ns,
        )
    finally:
        os.close(file_descriptor)


def _snapshot_file(path: Path, *, label: str) -> _FileSnapshot:
    target = _absolute_path(path)
    descriptor = _open_existing_directory_chain(target.parent)
    if descriptor is None:
        return _FileSnapshot(False)
    try:
        return _snapshot_file_at(descriptor, target, label=label)
    finally:
        os.close(descriptor)


def _snapshots_equal(left: _FileSnapshot, right: _FileSnapshot) -> bool:
    if left.exists != right.exists:
        return False
    if not left.exists:
        return True
    return (
        left.content == right.content
        and left.mode == right.mode
        and left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.modified_ns == right.modified_ns
    )


def _same_snapshot(path: Path, snapshot: _FileSnapshot) -> bool:
    current = _snapshot_file(path, label="desktop integration target")
    return _snapshots_equal(current, snapshot)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging desktop integration target")
        remaining = remaining[written:]


def _verify_rename_noreplace_at(descriptor: int) -> None:
    source_name = f".t1000-rename-probe-{uuid.uuid4().hex}"
    destination_name = f".t1000-rename-probe-{uuid.uuid4().hex}"
    source_exists = False
    destination_exists = False
    file_descriptor = -1
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | _require_nofollow()
    )
    try:
        file_descriptor = os.open(source_name, flags, 0o600, dir_fd=descriptor)
        source_exists = True
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = -1
        _rename_noreplace_at(
            descriptor,
            source_name,
            descriptor,
            destination_name,
        )
        source_exists = False
        destination_exists = True
        os.fsync(descriptor)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for name, exists in (
            (source_name, source_exists),
            (destination_name, destination_exists),
        ):
            if exists:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
        os.fsync(descriptor)


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int,
    expected: _FileSnapshot | None = None,
    parent_descriptor: int | None = None,
    probe: bool = False,
) -> _FileSnapshot:
    # Release-manager production publication uses this private hook as a
    # compatibility probe: instrumentation must observe the first-write
    # boundary, while the release transaction remains the owner of the real
    # staged rename and rollback journal.  Returning before opening a path is
    # deliberate; its anchored parent check runs immediately after the probe.
    if probe:
        return _FileSnapshot(True, content, mode, size=len(content))
    target = _absolute_path(path)
    owned_descriptor = parent_descriptor is None
    descriptor = parent_descriptor
    if descriptor is None:
        descriptor = _open_directory_chain(target.parent, create=True)
        if descriptor is None:  # pragma: no cover - create=True cannot return None.
            raise DesktopIntegrationError(
                f"could not create desktop integration parent safely: {target.parent}"
            )
    temporary_name = f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temporary_exists = False
    file_descriptor = -1
    published: _FileSnapshot | None = None
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | _require_nofollow()
    )
    try:
        try:
            file_descriptor = os.open(temporary_name, flags, mode, dir_fd=descriptor)
            temporary_exists = True
            os.fchmod(file_descriptor, mode)
            _write_all(file_descriptor, content)
            os.fsync(file_descriptor)
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not durably stage desktop integration target {target}: {exc}"
            ) from exc
        observed = _destination_stat(descriptor, target.name)
        _validate_destination_stat(target, observed, expected)
        try:
            staged = os.fstat(file_descriptor)
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            temporary_exists = False
            published = _FileSnapshot(
                True,
                content,
                stat.S_IMODE(staged.st_mode),
                staged.st_dev,
                staged.st_ino,
                staged.st_size,
                staged.st_mtime_ns,
            )
            final = _destination_stat(descriptor, target.name)
            if (
                final is None
                or final.st_dev != published.device
                or final.st_ino != published.inode
                or final.st_size != published.size
                or final.st_mtime_ns != published.modified_ns
            ):
                raise DesktopIntegrationError(
                    f"desktop integration target changed during publication: {target}"
                )
            os.fsync(descriptor)
            return published
        except Exception as exc:
            if published is not None:
                raise _PublishedWriteError(
                    f"could not publish desktop integration target durably {target}: {exc}",
                    published,
                ) from exc
            if isinstance(exc, DesktopIntegrationError):
                raise
            raise DesktopIntegrationError(
                f"could not publish desktop integration target durably {target}: {exc}"
            ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if owned_descriptor:
            os.close(descriptor)


def _target_lock_name(target: Path) -> str:
    digest = hashlib.sha256(os.fsencode(target.name)).hexdigest()[:24]
    return f".t1000-desktop-target-{digest}.lock"


def _acquire_target_lock(descriptor: int, target: Path) -> int:
    if fcntl is None:
        raise DesktopIntegrationError(
            "desktop integration requires per-target flock support"
        )
    lock_name = _target_lock_name(target)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | _require_nofollow()
    )
    try:
        lock_descriptor = os.open(lock_name, flags, 0o600, dir_fd=descriptor)
        metadata = os.fstat(lock_descriptor)
        observed = os.lstat(lock_name, dir_fd=descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
            or observed.st_dev != metadata.st_dev
            or observed.st_ino != metadata.st_ino
        ):
            raise DesktopIntegrationError(
                f"desktop integration target lock is unsafe: {target}"
            )
        os.fchmod(lock_descriptor, 0o600)
        os.fsync(descriptor)
        deadline = time.monotonic() + DESKTOP_TARGET_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DesktopIntegrationError(
                        f"timed out waiting for desktop integration target lock: {target}"
                    ) from exc
                time.sleep(min(DESKTOP_TARGET_LOCK_POLL_SECONDS, remaining))
        final = os.lstat(lock_name, dir_fd=descriptor)
        if final.st_dev != metadata.st_dev or final.st_ino != metadata.st_ino:
            raise DesktopIntegrationError(
                f"desktop integration target lock changed while acquiring it: {target}"
            )
        return lock_descriptor
    except Exception:
        if "lock_descriptor" in locals():
            os.close(lock_descriptor)
        raise


def _parent_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _revalidate_publication_parent(target: _PublicationTarget) -> None:
    descriptor = _open_directory_chain(target.path.parent, create=False)
    if descriptor is None:
        raise DesktopIntegrationError(
            f"desktop integration parent disappeared during publication: {target.path.parent}"
        )
    try:
        if _parent_identity(os.fstat(descriptor)) != _parent_identity(target.parent_metadata):
            raise DesktopIntegrationError(
                f"desktop integration parent identity changed during publication: {target.path.parent}"
            )
    finally:
        os.close(descriptor)


def _remove_regular_file(path: Path) -> None:
    target = _absolute_path(path)
    descriptor = _open_existing_directory_chain(target.parent)
    if descriptor is None:
        return
    try:
        observed = _destination_stat(descriptor, target.name)
        _validate_destination_stat(target, observed)
        if observed is None:
            return
        try:
            os.unlink(target.name, dir_fd=descriptor)
            os.fsync(descriptor)
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not remove desktop integration target durably {target}: {exc}"
            ) from exc
    finally:
        os.close(descriptor)


def _remove_uninstall_target(path: Path) -> bool:
    """Remove one managed desktop file beneath a validated parent chain."""

    target = _absolute_path(path)
    descriptor = _open_existing_directory_chain(target.parent)
    if descriptor is None:
        return False
    lock_descriptor = -1
    try:
        lock_descriptor = _acquire_target_lock(descriptor, target)
        _verify_rename_noreplace_at(descriptor)
        expected = _snapshot_file_at(
            descriptor,
            target,
            label="desktop integration uninstall target",
        )
        if not expected.exists:
            return False
        quarantine_name = f".{target.name}.uninstall-{uuid.uuid4().hex}"
        try:
            _rename_noreplace_at(
                descriptor,
                target.name,
                descriptor,
                quarantine_name,
            )
        except FileNotFoundError:
            return False
        quarantine_path = target.with_name(quarantine_name)
        try:
            quarantined = _snapshot_file_at(
                descriptor,
                quarantine_path,
                label="quarantined desktop integration uninstall target",
            )
        except Exception:
            _restore_quarantined_name_at(descriptor, target, quarantine_name)
            raise
        if not _snapshots_equal(quarantined, expected):
            _restore_quarantined_name_at(descriptor, target, quarantine_name)
            raise DesktopIntegrationError(
                f"desktop integration target changed before uninstall: {target}"
            )
        try:
            os.unlink(quarantine_name, dir_fd=descriptor)
            os.fsync(descriptor)
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not remove desktop integration target durably {target}: {exc}"
            ) from exc
        return True
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        os.close(descriptor)


def _stage_rollback_preimage_at(
    target: _PublicationTarget,
) -> str:
    assert target.preimage is not None and target.preimage.exists
    temporary_name = f".{target.path.name}.rollback-preimage-{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | _require_nofollow()
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            target.preimage.mode,
            dir_fd=target.parent_descriptor,
        )
        os.fchmod(descriptor, target.preimage.mode)
        _write_all(descriptor, target.preimage.content)
        os.fsync(descriptor)
        return temporary_name
    except Exception:
        try:
            os.unlink(temporary_name, dir_fd=target.parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_quarantined_name_at(
    parent_descriptor: int,
    public_path: Path,
    quarantine_name: str,
) -> None:
    try:
        _rename_noreplace_at(
            parent_descriptor,
            quarantine_name,
            parent_descriptor,
            public_path.name,
        )
    except FileExistsError as exc:
        quarantine_path = public_path.with_name(quarantine_name)
        raise DesktopIntegrationError(
            "desktop integration public target was concurrently reoccupied; "
            f"preserved displaced file at {quarantine_path}"
        ) from exc
    os.fsync(parent_descriptor)


def _restore_quarantined_name(
    target: _PublicationTarget,
    quarantine_name: str,
) -> None:
    _restore_quarantined_name_at(
        target.parent_descriptor,
        target.path,
        quarantine_name,
    )


def _restore_file_at(target: _PublicationTarget) -> None:
    if target.preimage is None or target.published is None:
        return
    quarantine_name = f".{target.path.name}.rollback-{uuid.uuid4().hex}"
    try:
        _rename_noreplace_at(
            target.parent_descriptor,
            target.path.name,
            target.parent_descriptor,
            quarantine_name,
        )
    except FileNotFoundError as exc:
        raise DesktopIntegrationError(
            f"desktop integration target disappeared before rollback: {target.path}"
        ) from exc

    quarantine_path = target.path.with_name(quarantine_name)
    try:
        quarantined = _snapshot_file_at(
            target.parent_descriptor,
            quarantine_path,
            label=f"quarantined {target.label}",
        )
    except Exception:
        _restore_quarantined_name(target, quarantine_name)
        raise
    if not _snapshots_equal(quarantined, target.published):
        _restore_quarantined_name(target, quarantine_name)
        raise DesktopIntegrationError(
            f"desktop integration target changed before rollback: {target.path}"
        )

    if not target.preimage.exists:
        os.unlink(quarantine_name, dir_fd=target.parent_descriptor)
        os.fsync(target.parent_descriptor)
        return

    staged_name = _stage_rollback_preimage_at(target)
    try:
        _rename_noreplace_at(
            target.parent_descriptor,
            staged_name,
            target.parent_descriptor,
            target.path.name,
        )
    except FileExistsError as exc:
        os.unlink(staged_name, dir_fd=target.parent_descriptor)
        os.unlink(quarantine_name, dir_fd=target.parent_descriptor)
        os.fsync(target.parent_descriptor)
        raise DesktopIntegrationError(
            f"desktop integration target was concurrently replaced during rollback: {target.path}"
        ) from exc
    except Exception:
        try:
            os.unlink(staged_name, dir_fd=target.parent_descriptor)
        except FileNotFoundError:
            pass
        _restore_quarantined_name(target, quarantine_name)
        raise
    os.unlink(quarantine_name, dir_fd=target.parent_descriptor)
    os.fsync(target.parent_descriptor)


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    target_path = _absolute_path(path)
    descriptor = _open_directory_chain(target_path.parent, create=True)
    if descriptor is None:  # pragma: no cover - create=True cannot return None.
        raise DesktopIntegrationError(f"could not restore desktop target: {target_path}")
    current = _snapshot_file_at(
        descriptor,
        target_path,
        label="desktop integration target",
    )
    if _snapshots_equal(current, snapshot):
        os.close(descriptor)
        return
    target = _PublicationTarget(
        path=target_path,
        content=b"",
        mode=snapshot.mode,
        label="desktop integration target",
        parent_descriptor=descriptor,
        parent_metadata=os.fstat(descriptor),
        preimage=snapshot,
        published=current,
    )
    try:
        _restore_file_at(target)
    finally:
        os.close(descriptor)


def _publish_files_transactionally(
    writes: tuple[tuple[Path, bytes, int, str], ...],
    *,
    finalize: Callable[[], object] | None = None,
    expected_preimages: Mapping[Path, tuple[bool, bytes, int]] | None = None,
) -> object | None:
    targets: list[_PublicationTarget] = []
    normalized_paths = [_absolute_path(path) for path, _content, _mode, _label in writes]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise DesktopIntegrationError("desktop integration transaction contains duplicate targets")
    expected = {
        _absolute_path(path): snapshot
        for path, snapshot in (expected_preimages or {}).items()
    }
    if not set(expected).issubset(normalized_paths):
        raise DesktopIntegrationError(
            "desktop integration expected preimage names an unknown target"
        )
    try:
        for (path, content, mode, label), normalized in zip(writes, normalized_paths):
            descriptor = _open_directory_chain(normalized.parent, create=True)
            if descriptor is None:  # pragma: no cover - create=True cannot return None.
                raise DesktopIntegrationError(
                    f"could not create desktop integration parent: {normalized.parent}"
                )
            targets.append(
                _PublicationTarget(
                    normalized,
                    content,
                    mode,
                    label,
                    descriptor,
                    os.fstat(descriptor),
                )
            )
        by_path = {target.path: target for target in targets}
        for path in sorted(by_path, key=os.fspath):
            target = by_path[path]
            target.lock_descriptor = _acquire_target_lock(
                target.parent_descriptor,
                target.path,
            )
        for descriptor in {target.parent_descriptor for target in targets}:
            _verify_rename_noreplace_at(descriptor)
        for target in targets:
            target.preimage = _snapshot_file_at(
                target.parent_descriptor,
                target.path,
                label=target.label,
            )
            expected_snapshot = expected.get(target.path)
            if expected_snapshot is not None and (
                target.preimage.exists != expected_snapshot[0]
                or (
                    target.preimage.exists
                    and (
                        target.preimage.content != expected_snapshot[1]
                        or target.preimage.mode != expected_snapshot[2]
                    )
                )
            ):
                raise DesktopIntegrationError(
                    f"desktop integration target changed before publication: {target.path}"
                )
        active: _PublicationTarget | None = None
        try:
            for target in targets:
                active = target
                target.published = _atomic_write_bytes(
                    target.path,
                    target.content,
                    mode=target.mode,
                    expected=target.preimage,
                    parent_descriptor=target.parent_descriptor,
                )
                _revalidate_publication_parent(target)
            for target in targets:
                _revalidate_publication_parent(target)
                current = _snapshot_file_at(
                    target.parent_descriptor,
                    target.path,
                    label=target.label,
                )
                if target.published is None or not _snapshots_equal(current, target.published):
                    raise DesktopIntegrationError(
                        f"desktop integration target changed after publication: {target.path}"
                    )
            return finalize() if finalize is not None else None
        except _PublishedWriteError as exc:
            if active is not None:
                active.published = exc.published
            raise
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(targets):
            if target.published is None:
                continue
            try:
                _restore_file_at(target)
            except Exception as rollback_exc:
                rollback_errors.append(f"{target.label}: {rollback_exc}")
        if rollback_errors:
            raise DesktopIntegrationError(
                f"desktop integration publication failed: {exc}; rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    finally:
        for target in reversed(targets):
            if target.lock_descriptor >= 0:
                os.close(target.lock_descriptor)
            os.close(target.parent_descriptor)


def build_install_plan(
    *,
    command: str | None = None,
    data_home: str | Path | None = None,
    config_home: str | Path | None = None,
) -> DesktopInstallPlan:
    exec_command = resolve_launch_command(command)
    _command_launcher(exec_command)
    return DesktopInstallPlan(
        command=exec_command,
        paths=desktop_paths(data_home, config_home),
        desktop_entry=build_desktop_entry(exec_command),
        autostart_entry=build_autostart_entry(),
    )


def _update_desktop_database(updater: str, applications_dir: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            [updater, str(applications_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=DESKTOP_DATABASE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise _DesktopDatabaseUpdateError(
            "desktop database update timed out",
            {
                "returncode": None,
                "timed_out": True,
                "executed": True,
                "external_state": "unknown",
            },
        ) from exc
    except FileNotFoundError as exc:
        raise _DesktopDatabaseUpdateError(
            "desktop database updater disappeared before execution",
            {
                "returncode": None,
                "timed_out": False,
                "executed": False,
                "external_state": "unchanged",
            },
        ) from exc
    except OSError as exc:
        raise _DesktopDatabaseUpdateError(
            f"desktop database updater could not execute: {exc}",
            {
                "returncode": None,
                "timed_out": False,
                "executed": False,
                "external_state": "unknown",
            },
        ) from exc
    status = {
        "returncode": result.returncode,
        "timed_out": False,
        "executed": True,
        "external_state": "updated" if result.returncode == 0 else "unknown",
    }
    if result.returncode != 0:
        raise _DesktopDatabaseUpdateError("desktop database update failed", status)
    return status


def install_desktop(
    plan: DesktopInstallPlan,
    *,
    dry_run: bool = False,
    update_database: bool = False,
    install_autostart: bool = False,
) -> dict[str, object]:
    payload = plan.as_dict()
    payload.update(
        {
            "action": "install-desktop",
            "dry_run": dry_run,
            "ok": True,
            "written": [],
            "autostart_requested": install_autostart,
        }
    )
    if install_autostart and _release_manager_owns_autostart(
        plan.paths.autostart_file
    ):
        payload["ok"] = False
        payload["error"] = (
            "canonical autostart is owned by production release install/rollback receipts"
        )
        return payload
    if install_autostart:
        ready, detail = managed_autostart_ready(
            launcher=MANAGED_LAUNCHER,
        )
        payload["autostart_ready"] = ready
        payload["autostart_detail"] = detail
        if not ready:
            payload["ok"] = False
            payload["error"] = "refusing autostart install until managed SSD launcher is verified"
            return payload
    if dry_run:
        return payload

    writes = [
        (plan.paths.desktop_file, plan.desktop_entry.encode("utf-8"), 0o755, "desktop entry"),
        (plan.paths.icon_file, plan.icon_svg.encode("utf-8"), 0o644, "desktop icon"),
    ]
    if install_autostart:
        writes.append(
            (
                plan.paths.autostart_file,
                plan.autostart_entry.encode("utf-8"),
                0o644,
                "desktop autostart entry",
            )
        )
    updater = shutil.which("update-desktop-database") if update_database else None
    finalize = (
        lambda: _update_desktop_database(updater, plan.paths.applications_dir)
        if updater is not None
        else None
    )
    try:
        database_status = _publish_files_transactionally(
            tuple(writes),
            finalize=finalize if updater is not None else None,
        )
    except _DesktopDatabaseUpdateError as exc:
        payload["ok"] = False
        payload["error"] = str(exc)
        payload["update_desktop_database"] = exc.status
        payload["rollback"] = {
            "desktop_files": "restored",
            "desktop_database": exc.status["external_state"],
        }
        return payload
    payload["written"] = [str(path) for path, _content, _mode, _label in writes]
    if database_status is not None:
        payload["update_desktop_database"] = database_status
    return payload


def uninstall_desktop(
    *,
    data_home: str | Path | None = None,
    config_home: str | Path | None = None,
    dry_run: bool = False,
    remove_autostart: bool = False,
) -> dict[str, object]:
    paths = desktop_paths(data_home, config_home)
    targets = [paths.desktop_file, paths.icon_file]
    if remove_autostart:
        targets.append(paths.autostart_file)
    payload = {
        "action": "uninstall-desktop",
        "dry_run": dry_run,
        "ok": True,
        "targets": [str(target) for target in targets],
        "removed": [],
        "autostart_requested": remove_autostart,
    }
    if remove_autostart and _release_manager_owns_autostart(paths.autostart_file):
        payload["ok"] = False
        payload["error"] = (
            "canonical autostart is owned by production release install/rollback receipts"
        )
        return payload
    if dry_run:
        return payload
    removed: list[str] = []
    for target in targets:
        try:
            if _remove_uninstall_target(target):
                removed.append(str(target))
        except FileNotFoundError:
            continue
    payload["removed"] = removed
    return payload
