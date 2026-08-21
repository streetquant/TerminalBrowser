"""Keep the tmux server outside the graphical application's lifecycle.

The first ``tmux new-session`` command daemonizes a server in the caller's
cgroup.  When the caller is a transient GUI service with ``KillMode=control-
group``, stopping the GUI also kills the tmux server and every terminal session.
This module gives the server an independent user-systemd lifecycle:

* first-server commands run in a delegated transient scope; and
* legacy servers are moved, with PID/start-time revalidation, into a delegated
  process-only anchor service.

All helpers are argv-based and fail closed on identity drift.  Systems without a
user systemd manager keep the historical direct-tmux behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")
_BUSCTL = Path("/usr/bin/busctl")

_SERVER_START_COMMANDS = frozenset({"new-session", "start-server"})
_SCOPE_UNIT = re.compile(r"^t1000-tmux-server-[A-Za-z0-9_.-]+\.scope\Z")
_ANCHOR_UNIT = re.compile(r"^t1000-tmux-anchor(?:-[0-9]+-[0-9]+)?\.service\Z")

_ANCHOR_MONITOR = r"""
from pathlib import Path
import sys
import time

pid = int(sys.argv[1])
expected_start = int(sys.argv[2])
while True:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        break
    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    try:
        observed_start = int(fields[19])
    except (IndexError, TypeError, ValueError):
        break
    if observed_start != expected_start:
        break
    time.sleep(1.0)
""".strip()


class TmuxServerIsolationError(RuntimeError):
    """The tmux server could not be isolated without losing identity."""


@dataclass(frozen=True)
class TmuxServerIsolationResult:
    ok: bool
    server_present: bool
    changed: bool
    server_pid: int | None
    server_start_ticks: int | None
    unit: str | None
    cgroup: str | None
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "server_present": self.server_present,
            "changed": self.changed,
            "server_pid": self.server_pid,
            "server_start_ticks": self.server_start_ticks,
            "unit": self.unit,
            "cgroup": self.cgroup,
            "detail": self.detail,
        }


def _executable(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved.is_file() and os.access(resolved, os.X_OK)


def user_systemd_scope_available() -> bool:
    """Return whether a user manager can create delegated transient units."""

    if not (_executable(_SYSTEMD_RUN) and _executable(_SYSTEMCTL) and _executable(_BUSCTL)):
        return False
    runtime = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
    ).expanduser()
    return (runtime / "systemd" / "private").exists()


def _socket_root_visible_to_user_manager(*, timeout: float = 2.0) -> bool:
    """Prove the tmux socket root is shared with user-systemd's mount namespace."""

    raw_root = os.environ.get("TMUX_TMPDIR", "/tmp").strip() or "/tmp"
    root = Path(raw_root).expanduser()
    try:
        if not root.is_dir():
            return False
        descriptor, raw_sentinel = tempfile.mkstemp(
            prefix=".t1000-mount-probe-",
            dir=root,
        )
        os.close(descriptor)
        sentinel = Path(raw_sentinel)
    except OSError:
        return False
    try:
        result = _run(
            [
                str(_SYSTEMD_RUN),
                "--user",
                "--wait",
                "--collect",
                "--quiet",
                "--pipe",
                "/usr/bin/test",
                "-f",
                str(sentinel),
            ],
            timeout=timeout,
        )
        return result.returncode == 0
    except TmuxServerIsolationError:
        return False
    finally:
        try:
            sentinel.unlink()
        except OSError:
            pass


def scoped_tmux_server_command(
    command: Sequence[str],
    *,
    owner_pid: int | None = None,
) -> list[str] | None:
    """Wrap a server-starting tmux command in an independent delegated scope."""

    argv = [str(item) for item in command]
    if len(argv) < 2 or argv[1] not in _SERVER_START_COMMANDS:
        return None
    if not user_systemd_scope_available():
        return None
    if not _socket_root_visible_to_user_manager():
        return None
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    nonce = time.monotonic_ns()
    unit = f"t1000-tmux-server-{pid}-{nonce:x}.scope"
    return [
        str(_SYSTEMD_RUN),
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--property=Delegate=yes",
        *argv,
    ]


def _run(
    argv: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(item) for item in argv],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(0.05, float(timeout)),
        )
    except subprocess.TimeoutExpired as exc:
        raise TmuxServerIsolationError(
            f"lifecycle command timed out after {timeout:g}s: {argv[0] if argv else 'command'}"
        ) from exc


def _tmux_server_pid(tmux_executable: str, *, timeout: float) -> int | None:
    result = _run(
        [tmux_executable, "display-message", "-p", "#{pid}"],
        timeout=timeout,
    )
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError as exc:
        raise TmuxServerIsolationError("tmux returned an invalid server PID") from exc
    if pid <= 1:
        raise TmuxServerIsolationError("tmux returned an unsafe server PID")
    return pid


def _process_identity(pid: int) -> tuple[str, int] | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    if len(fields) <= 19:
        return None
    try:
        return fields[0], int(fields[19])
    except (TypeError, ValueError):
        return None


def _process_cgroup(pid: int) -> str | None:
    try:
        return Path(f"/proc/{int(pid)}/cgroup").read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
    except OSError:
        return None


def _unit_from_cgroup(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    for line in cgroup.splitlines():
        path = line.split(":", 2)[-1].strip()
        if not path:
            continue
        unit = Path(path).name
        if unit.endswith((".scope", ".service")):
            return unit
    return None


def _unit_properties(unit: str, *, timeout: float) -> dict[str, str] | None:
    result = _run(
        [
            str(_SYSTEMCTL),
            "--user",
            "show",
            unit,
            "--property=Id,ActiveState,SubState,MainPID,ControlGroup,Delegate,KillMode",
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        return None
    return {
        key: value
        for line in result.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _properties_bind_unit(
    properties: dict[str, str] | None,
    unit: str,
) -> bool:
    if not properties or properties.get("Id") != unit:
        return False
    control_group = properties.get("ControlGroup", "").rstrip("/")
    return bool(control_group and Path(control_group).name == unit)


def _anchor_properties_are_safe(
    properties: dict[str, str] | None,
    unit: str,
) -> bool:
    if not _properties_bind_unit(properties, unit):
        return False
    try:
        main_pid = int(properties.get("MainPID", "0"))
    except (AttributeError, ValueError):
        return False
    return (
        properties.get("ActiveState") == "active"
        and properties.get("SubState") == "running"
        and properties.get("Delegate") == "yes"
        and properties.get("KillMode") == "process"
        and main_pid > 1
    )


def _ensure_anchor_unit(
    unit: str,
    *,
    server_pid: int,
    server_start_ticks: int,
    timeout: float,
) -> tuple[dict[str, str], bool]:
    existing = _unit_properties(unit, timeout=timeout)
    if _anchor_properties_are_safe(existing, unit):
        return existing or {}, False
    result = _run(
        [
            str(_SYSTEMD_RUN),
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=Type=exec",
            "--property=KillMode=process",
            "--property=Delegate=yes",
            "--description=T1000 tmux server lifecycle anchor",
            sys.executable,
            "-I",
            "-B",
            "-c",
            _ANCHOR_MONITOR,
            str(server_pid),
            str(server_start_ticks),
        ],
        timeout=timeout,
    )
    properties = _unit_properties(unit, timeout=timeout)
    if result.returncode != 0 and not _anchor_properties_are_safe(properties, unit):
        detail = result.stderr.strip() or result.stdout.strip() or "unknown systemd-run failure"
        raise TmuxServerIsolationError(
            f"could not create delegated tmux anchor {unit}: {detail}"
        )
    if not _anchor_properties_are_safe(properties, unit):
        raise TmuxServerIsolationError(
            f"delegated tmux anchor {unit} did not become active"
        )
    return properties or {}, True


def _stop_anchor_unit(unit: str, *, timeout: float) -> None:
    """Best-effort cleanup for an anchor that never received the server."""

    _run(
        [str(_SYSTEMCTL), "--user", "stop", unit],
        timeout=timeout,
    )
    _run(
        [str(_SYSTEMCTL), "--user", "reset-failed", unit],
        timeout=timeout,
    )


def _attach_process_to_unit(unit: str, pid: int, *, timeout: float) -> None:
    result = _run(
        [
            str(_BUSCTL),
            "--user",
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "AttachProcessesToUnit",
            "ssau",
            unit,
            "",
            "1",
            str(int(pid)),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown D-Bus failure"
        raise TmuxServerIsolationError(
            f"could not move tmux server into {unit}: {detail}"
        )


def _dedicated_unit_is_safe(unit: str | None, *, timeout: float) -> bool:
    if unit is None:
        return False
    if _SCOPE_UNIT.fullmatch(unit):
        properties = _unit_properties(unit, timeout=timeout)
        return bool(
            _properties_bind_unit(properties, unit)
            and properties.get("ActiveState") == "active"
            and properties.get("SubState") == "running"
            and properties.get("Delegate") == "yes"
        )
    if _ANCHOR_UNIT.fullmatch(unit):
        return _anchor_properties_are_safe(
            _unit_properties(unit, timeout=timeout),
            unit,
        )
    return False


def isolate_existing_tmux_server(
    tmux_executable: str,
    *,
    query_timeout: float = 0.35,
    timeout: float = 2.0,
) -> TmuxServerIsolationResult:
    """Move a legacy live tmux server out of the caller's service cgroup."""

    pid = _tmux_server_pid(tmux_executable, timeout=query_timeout)
    if pid is None:
        return TmuxServerIsolationResult(
            ok=True,
            server_present=False,
            changed=False,
            server_pid=None,
            server_start_ticks=None,
            unit=None,
            cgroup=None,
            detail="no tmux server is running",
        )
    identity = _process_identity(pid)
    if identity is None:
        raise TmuxServerIsolationError(
            "tmux server process identity is unavailable"
        )
    _state, start_ticks = identity
    cgroup = _process_cgroup(pid)
    unit = _unit_from_cgroup(cgroup)
    if _dedicated_unit_is_safe(unit, timeout=timeout):
        return TmuxServerIsolationResult(
            ok=True,
            server_present=True,
            changed=False,
            server_pid=pid,
            server_start_ticks=start_ticks,
            unit=unit,
            cgroup=cgroup,
            detail="tmux server already has an independent lifecycle",
        )
    if not user_systemd_scope_available():
        return TmuxServerIsolationResult(
            ok=False,
            server_present=True,
            changed=False,
            server_pid=pid,
            server_start_ticks=start_ticks,
            unit=unit,
            cgroup=cgroup,
            detail="user systemd is unavailable; tmux server lifecycle was not changed",
        )

    anchor = f"t1000-tmux-anchor-{pid}-{start_ticks}.service"
    _properties, anchor_created = _ensure_anchor_unit(
        anchor,
        server_pid=pid,
        server_start_ticks=start_ticks,
        timeout=timeout,
    )
    attached = False
    try:
        pre_attach_pid = _tmux_server_pid(
            tmux_executable,
            timeout=query_timeout,
        )
        pre_attach_identity = _process_identity(pid)
        if (
            pre_attach_pid != pid
            or pre_attach_identity is None
            or pre_attach_identity[1] != start_ticks
        ):
            raise TmuxServerIsolationError(
                "tmux server process identity changed before lifecycle attachment"
            )
        _attach_process_to_unit(anchor, pid, timeout=timeout)
        attached = True
    except Exception:
        if anchor_created and not attached:
            try:
                _stop_anchor_unit(anchor, timeout=timeout)
            except TmuxServerIsolationError:
                pass
        raise

    observed_pid = _tmux_server_pid(tmux_executable, timeout=query_timeout)
    observed_identity = _process_identity(pid)
    observed_cgroup = _process_cgroup(pid)
    if observed_pid != pid or observed_identity is None:
        raise TmuxServerIsolationError(
            "tmux server disappeared while its lifecycle was being isolated"
        )
    if observed_identity[1] != start_ticks:
        raise TmuxServerIsolationError(
            "tmux server process identity changed during lifecycle isolation"
        )
    observed_unit = _unit_from_cgroup(observed_cgroup)
    if observed_unit != anchor or not _dedicated_unit_is_safe(
        anchor,
        timeout=timeout,
    ):
        raise TmuxServerIsolationError(
            "tmux server did not enter the verified delegated anchor"
        )
    return TmuxServerIsolationResult(
        ok=True,
        server_present=True,
        changed=True,
        server_pid=pid,
        server_start_ticks=start_ticks,
        unit=anchor,
        cgroup=observed_cgroup,
        detail="tmux server moved into an independent delegated anchor",
    )
