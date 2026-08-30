from __future__ import annotations

import os
import shlex
import stat
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

APP_ID = "io.github.t1000.T1000"
DESKTOP_FILENAME = f"{APP_ID}.desktop"
AUTOSTART_FILENAME = DESKTOP_FILENAME
ICON_NAME = "t1000"
MANAGED_LAUNCHER = Path("/home/arch/.local/bin/t1000")
MANAGED_ROOT_ENV = "T1000_MANAGED_ROOT"
DEFAULT_MANAGED_ROOT = Path(
    "/home/arch/.local/state/codex-project-homes/runtime/t1000-managed"
)
AUTOSTART_RESOURCE = "io.github.t1000.T1000.autostart.desktop"

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
        launcher = _command_launcher(self.command)
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
            "managed_launcher": str(launcher),
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


def xdg_data_home(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    raw = env.get("XDG_DATA_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def xdg_config_home(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    raw = env.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config"


def desktop_paths(
    data_home: str | Path | None = None,
    config_home: str | Path | None = None,
) -> DesktopPaths:
    root = Path(data_home).expanduser() if data_home is not None else xdg_data_home()
    config_root = (
        Path(config_home).expanduser()
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
        "Name=T1000",
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
    if any(character in exec_command for character in "\x00\r\n"):
        raise DesktopIntegrationError(
            "desktop command contains an unsafe control character"
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


def build_autostart_entry(exec_command: str | None = None) -> str:
    """Return the managed GUI autostart contract.

    The entry deliberately contains no shell, environment assignment, or
    alternate executable.  Desktop-session launchers pass through the current
    graphical DBus/display environment, while the managed T1000 launcher is
    responsible for release and SSD-root verification.
    """

    resolved_command = (
        resolve_launch_command(str(MANAGED_LAUNCHER))
        if exec_command is None
        else exec_command
    )
    launcher = _command_launcher(resolved_command)
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
    return rendered


def _managed_root() -> Path:
    raw = os.environ.get(MANAGED_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_MANAGED_ROOT


def _non_rotational_device(path: Path) -> bool:
    try:
        device = path.stat().st_dev
        rotational = Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}" / "queue" / "rotational"
        return rotational.read_text(encoding="ascii").strip() == "0"
    except (OSError, UnicodeError, ValueError):
        return False


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

    launcher_path = Path(launcher).expanduser().absolute()
    root = (
        Path(managed_root).expanduser().absolute()
        if managed_root is not None
        else _managed_root().absolute()
    )
    storage_root = root.parent
    bootstrap = root / "bootstrap.py"
    try:
        launcher_stat = launcher_path.lstat()
        root_stat = root.lstat()
        storage_stat = storage_root.lstat()
        bootstrap_stat = bootstrap.lstat()
    except OSError as exc:
        return False, f"managed launcher or storage root unavailable: {exc}"
    if stat.S_ISLNK(launcher_stat.st_mode) or not stat.S_ISREG(launcher_stat.st_mode):
        return False, "managed launcher is not an executable regular file"
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return False, "managed release root is not a directory"
    if stat.S_ISLNK(storage_stat.st_mode) or not stat.S_ISDIR(storage_stat.st_mode):
        return False, "managed storage root is not a directory"
    if stat.S_ISLNK(bootstrap_stat.st_mode) or not stat.S_ISREG(bootstrap_stat.st_mode):
        return False, "managed release root is not a directory"
    if not os.access(launcher_path, os.X_OK):
        return False, "managed launcher is not an executable regular file"
    if (
        root_stat.st_dev != storage_stat.st_dev
        or bootstrap_stat.st_dev != storage_stat.st_dev
    ):
        return False, "managed release root and bootstrap are not on one storage device"
    try:
        launcher_text = launcher_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"managed launcher cannot be inspected: {exc}"
    if str(bootstrap) not in launcher_text:
        return False, "managed launcher does not target the selected SSD bootstrap"
    try:
        from .storage_fabric import attest_storage_fabric

        attestation = attest_storage_fabric(
            managed_root=root,
            launcher_path=launcher_path,
            production=True,
        )
    except Exception as exc:
        return False, f"storage attestation could not be completed: {exc}"
    if not attestation.ok:
        return False, attestation.reason
    return True, attestation.reason


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _ensure_real_directory(path: Path) -> Path:
    directory = _absolute_path(path)
    missing: list[Path] = []
    current = directory
    while True:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            missing.append(current)
            mode = None
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not inspect desktop integration directory: {current}: {exc}"
            ) from exc
        if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISDIR(mode)):
            raise DesktopIntegrationError(
                f"desktop integration parent is not a real directory: {current}"
            )
        if current.parent == current:
            break
        current = current.parent
    for current in reversed(missing):
        try:
            current.mkdir(mode=0o755)
        except FileExistsError:
            pass
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not create desktop integration directory: {current}: {exc}"
            ) from exc
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not inspect desktop integration directory: {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise DesktopIntegrationError(
                f"desktop integration parent is not a real directory: {current}"
            )
    return directory


def _open_real_directory(path: Path) -> int:
    directory = _absolute_path(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise DesktopIntegrationError(
            f"could not open desktop integration directory safely: {directory}: {exc}"
        ) from exc
    try:
        mode = os.fstat(descriptor).st_mode
    except OSError:
        os.close(descriptor)
        raise
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        os.close(descriptor)
        raise DesktopIntegrationError(
            f"desktop integration parent is not a real directory: {directory}"
        )
    return descriptor


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


def _snapshot_file(path: Path, *, label: str) -> _FileSnapshot:
    target = _absolute_path(path)
    descriptor = _open_real_directory(target.parent)
    try:
        observed = _destination_stat(descriptor, target.name)
        _validate_destination_stat(target, observed)
        if observed is None:
            return _FileSnapshot(False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(target.name, flags, dir_fd=descriptor)
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not open existing {label} safely: {target}: {exc}"
            ) from exc
        try:
            opened = os.fstat(file_descriptor)
            if (
                stat.S_ISLNK(opened.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != observed.st_dev
                or opened.st_ino != observed.st_ino
            ):
                raise DesktopIntegrationError(
                    f"existing {label} changed to an unsafe target: {target}"
                )
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                content = handle.read()
            final = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
            if final.st_dev != observed.st_dev or final.st_ino != observed.st_ino:
                raise DesktopIntegrationError(
                    f"existing {label} changed while being read: {target}"
                )
            return _FileSnapshot(
                True,
                content,
                stat.S_IMODE(final.st_mode),
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
            )
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _same_snapshot(path: Path, snapshot: _FileSnapshot) -> bool:
    current = _snapshot_file(path, label="desktop integration target")
    if current.exists != snapshot.exists:
        return False
    if not current.exists:
        return True
    return (
        current.content == snapshot.content
        and current.mode == snapshot.mode
        and current.device == snapshot.device
        and current.inode == snapshot.inode
    )


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int,
    expected: _FileSnapshot | None = None,
) -> None:
    target = _absolute_path(path)
    parent = _ensure_real_directory(target.parent)
    descriptor = _open_real_directory(parent)
    temporary_name = f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temporary_exists = False
    file_descriptor = -1
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            file_descriptor = os.open(temporary_name, flags, mode, dir_fd=descriptor)
            temporary_exists = True
            os.fchmod(file_descriptor, mode)
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise DesktopIntegrationError(
                f"could not durably stage desktop integration target {target}: {exc}"
            ) from exc
        observed = _destination_stat(descriptor, target.name)
        _validate_destination_stat(target, observed, expected)
        try:
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            temporary_exists = False
            os.fsync(descriptor)
        except OSError as exc:
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
        os.close(descriptor)


def _remove_regular_file(path: Path) -> None:
    target = _absolute_path(path)
    descriptor = _open_real_directory(target.parent)
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


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    if _same_snapshot(path, snapshot):
        return
    if snapshot.exists:
        _atomic_write_bytes(
            path,
            snapshot.content,
            mode=snapshot.mode,
        )
    else:
        _remove_regular_file(path)


def _publish_files_transactionally(
    writes: tuple[tuple[Path, bytes, int, str], ...],
) -> None:
    snapshots: list[tuple[Path, _FileSnapshot, str]] = []
    for path, _content, _mode, label in writes:
        target = _absolute_path(path)
        _ensure_real_directory(target.parent)
        snapshots.append((target, _snapshot_file(target, label=label), label))
    try:
        for path, content, mode, _label in writes:
            target = _absolute_path(path)
            snapshot = next(
                saved for saved_path, saved, _ in snapshots if saved_path == target
            )
            if not _same_snapshot(target, snapshot):
                raise DesktopIntegrationError(
                    f"desktop integration destination changed before publication: {target}"
                )
            _atomic_write_bytes(target, content, mode=mode)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, snapshot, label in reversed(snapshots):
            try:
                _restore_file(path, snapshot)
            except Exception as rollback_exc:
                rollback_errors.append(f"{label}: {rollback_exc}")
        if rollback_errors:
            raise DesktopIntegrationError(
                f"desktop integration publication failed: {exc}; rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


def build_install_plan(
    *,
    command: str | None = None,
    data_home: str | Path | None = None,
    config_home: str | Path | None = None,
) -> DesktopInstallPlan:
    exec_command = resolve_launch_command(command)
    return DesktopInstallPlan(
        command=exec_command,
        paths=desktop_paths(data_home, config_home),
        desktop_entry=build_desktop_entry(exec_command),
        autostart_entry=build_autostart_entry(exec_command),
    )


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
    if install_autostart:
        ready, detail = managed_autostart_ready(
            launcher=_command_launcher(plan.command),
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
    _publish_files_transactionally(tuple(writes))
    payload["written"] = [str(path) for path, _content, _mode, _label in writes]

    if update_database:
        updater = shutil.which("update-desktop-database")
        if updater:
            result = subprocess.run([updater, str(plan.paths.applications_dir)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            payload["update_desktop_database"] = {"returncode": result.returncode, "stderr": result.stderr.strip()}
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
    if dry_run:
        return payload
    removed: list[str] = []
    for target in targets:
        try:
            target.unlink()
            removed.append(str(target))
        except FileNotFoundError:
            continue
    payload["removed"] = removed
    return payload
