from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

USER_UNIT_NAME = "termdeck-checkpoint.service"
SYSTEM_UNIT_NAME = "termdeck-checkpoint.service"
DEFAULT_USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
DEFAULT_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
_SAFE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class ShutdownServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShutdownServiceResult:
    action: str
    path: Path
    enabled: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "path": str(self.path),
            "enabled": self.enabled,
            "detail": self.detail,
        }


def _resolve_t1000_executable(executable: str | Path | None = None) -> Path:
    if executable is not None:
        candidate = Path(executable).expanduser()
    else:
        discovered = shutil.which("t1000")
        candidate = Path(discovered) if discovered else Path(sys.argv[0])
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ShutdownServiceError(f"TermDeck executable not found: {candidate}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ShutdownServiceError(f"TermDeck executable is not runnable: {candidate}")
    if "\n" in str(candidate) or "\r" in str(candidate):
        raise ShutdownServiceError("TermDeck executable path contains a newline")
    return candidate


def _systemd_quote(value: str | Path) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_user_checkpoint_service(executable: str | Path) -> str:
    command = _systemd_quote(executable)
    return f"""[Unit]
Description=Persist TermDeck sessions before user-manager shutdown
Documentation=https://github.com/Rebel0789/t1000
Before=exit.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/true
ExecStop=-{command} checkpoint --all
TimeoutStopSec=15min
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def render_system_checkpoint_service(
    executable: str | Path,
    *,
    user: str,
    uid: int,
    home: str | Path,
    required_mounts: tuple[str | Path, ...] = (),
) -> str:
    """Render a system unit ordered before the complete user manager stops."""

    if not _SAFE_USER.fullmatch(user):
        raise ShutdownServiceError(f"unsafe user name: {user!r}")
    if int(uid) < 0:
        raise ShutdownServiceError(f"invalid uid: {uid}")
    home_path = Path(home).expanduser()
    command = _systemd_quote(executable)
    mounts = tuple(dict.fromkeys(str(Path(path)) for path in (home_path, *required_mounts)))
    mount_line = "RequiresMountsFor=" + " ".join(_systemd_quote(path) for path in mounts)
    path_value = f"{home_path}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
    return f"""[Unit]
Description=Persist TermDeck sessions before the user manager stops
Documentation=https://github.com/Rebel0789/t1000
After=user@{int(uid)}.service local-fs.target
{mount_line}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/true
ExecStop=-/usr/bin/runuser -u {user} -- /usr/bin/env HOME={_systemd_quote(home_path)} PATH={_systemd_quote(path_value)} {command} checkpoint --all
TimeoutStopSec=15min
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _run_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShutdownServiceError(f"systemctl --user failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "systemctl --user failed").strip()
        raise ShutdownServiceError(detail)
    return completed


def _run_systemctl_system(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["systemctl", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShutdownServiceError(f"systemctl failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "systemctl failed").strip()
        raise ShutdownServiceError(detail)
    return completed


def install_user_checkpoint_service(
    *,
    executable: str | Path | None = None,
    unit_dir: str | Path | None = None,
    enable: bool = True,
) -> ShutdownServiceResult:
    resolved = _resolve_t1000_executable(executable)
    directory = Path(unit_dir).expanduser() if unit_dir else DEFAULT_USER_UNIT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / USER_UNIT_NAME
    content = render_user_checkpoint_service(resolved)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    if enable:
        _run_systemctl(["daemon-reload"])
        _run_systemctl(["enable", "--now", USER_UNIT_NAME])
    return ShutdownServiceResult(
        "install",
        path,
        enable,
        "shutdown checkpoint service installed" if enable else "shutdown checkpoint unit written",
    )


def uninstall_user_checkpoint_service(
    *,
    unit_dir: str | Path | None = None,
    disable: bool = True,
) -> ShutdownServiceResult:
    directory = Path(unit_dir).expanduser() if unit_dir else DEFAULT_USER_UNIT_DIR
    path = directory / USER_UNIT_NAME
    if disable:
        try:
            _run_systemctl(["disable", "--now", USER_UNIT_NAME])
        except ShutdownServiceError as exc:
            # Removing an already absent/inactive unit remains idempotent, but a
            # live unit-manager failure must still be visible to the caller.
            if path.exists():
                raise
            detail = str(exc)
        else:
            detail = "shutdown checkpoint service disabled"
    else:
        detail = "shutdown checkpoint unit removal requested"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if disable:
        _run_systemctl(["daemon-reload"])
    return ShutdownServiceResult("uninstall", path, False, detail)


def install_system_checkpoint_service(
    *,
    executable: str | Path,
    user: str,
    uid: int,
    home: str | Path,
    required_mounts: tuple[str | Path, ...] = (),
    unit_dir: str | Path | None = None,
    enable: bool = True,
) -> ShutdownServiceResult:
    """Install the strict shutdown-ordered system service.

    Callers must run this operation as root. The unit is ordered after the
    complete ``user@UID.service`` while active, which reverses on shutdown and
    guarantees the checkpoint completes before the user manager and its tmux
    scopes are terminated.
    """

    if os.geteuid() != 0:
        raise ShutdownServiceError("system checkpoint service installation requires root")
    resolved = _resolve_t1000_executable(executable)
    directory = Path(unit_dir) if unit_dir else DEFAULT_SYSTEM_UNIT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SYSTEM_UNIT_NAME
    content = render_system_checkpoint_service(
        resolved,
        user=user,
        uid=uid,
        home=home,
        required_mounts=required_mounts,
    )
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    if enable:
        _run_systemctl_system(["daemon-reload"])
        _run_systemctl_system(["enable", "--now", SYSTEM_UNIT_NAME])
    return ShutdownServiceResult(
        "install-system",
        path,
        enable,
        "strict shutdown-ordered checkpoint service installed"
        if enable
        else "strict checkpoint unit written",
    )


def uninstall_system_checkpoint_service(
    *,
    unit_dir: str | Path | None = None,
    disable: bool = True,
) -> ShutdownServiceResult:
    if os.geteuid() != 0:
        raise ShutdownServiceError("system checkpoint service removal requires root")
    directory = Path(unit_dir) if unit_dir else DEFAULT_SYSTEM_UNIT_DIR
    path = directory / SYSTEM_UNIT_NAME
    if disable:
        try:
            _run_systemctl_system(["disable", "--now", SYSTEM_UNIT_NAME])
        except ShutdownServiceError:
            if path.exists():
                raise
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if disable:
        _run_systemctl_system(["daemon-reload"])
    return ShutdownServiceResult(
        "uninstall-system",
        path,
        False,
        "strict shutdown-ordered checkpoint service removed",
    )
