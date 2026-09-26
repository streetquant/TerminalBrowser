"""Keep the tmux server outside the graphical application's lifecycle.

The first ``tmux new-session`` command daemonizes a server in the caller's
cgroup.  When the caller is a transient GUI service with ``KillMode=control-
group``, stopping the GUI also kills the tmux server and every terminal session.
This module admits an independent user-systemd lifecycle:

* first-server commands run in a delegated transient scope; and
* servers already in a verified delegated scope or process-only anchor are
  accepted without changing them.

Legacy servers remain untouched when safe migration is unavailable. A numeric
PID attachment can move an unrelated process if the original server exits and
its PID is reused. Sampling start ticks cannot bind that mutation. Systems
without a user systemd manager keep the historical direct-tmux start behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .storage_fabric import StorageFabricError, tmux_subprocess_environment

_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")

_SERVER_START_COMMANDS = frozenset({"new-session", "start-server"})
_SCOPE_UNIT = re.compile(r"^t1000-tmux-server-[A-Za-z0-9_.-]+\.scope\Z")
_ANCHOR_UNIT = re.compile(r"^t1000-tmux-anchor(?:-[0-9]+-[0-9]+)?\.service\Z")


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

    if not (_executable(_SYSTEMD_RUN) and _executable(_SYSTEMCTL)):
        return False
    runtime = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
    ).expanduser()
    return (runtime / "systemd" / "private").exists()


def scoped_tmux_server_command(
    command: Sequence[str],
    *,
    owner_pid: int | None = None,
) -> list[str] | None:
    """Wrap a first-server command while retaining the caller's mount namespace.

    ``systemd-run --scope`` executes the command locally, so a socket directory
    private to this process does not need to be visible from a manager-launched
    service. Probing that different namespace would incorrectly force an
    unisolated direct start.
    """

    argv = [str(item) for item in command]
    if len(argv) < 2 or argv[1] not in _SERVER_START_COMMANDS:
        return None
    if not user_systemd_scope_available():
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
        child_environment = tmux_subprocess_environment(materialize=True)
    except StorageFabricError as exc:
        raise TmuxServerIsolationError(str(exc)) from exc
    try:
        return subprocess.run(
            [str(item) for item in argv],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(0.05, float(timeout)),
            env=child_environment,
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


def _unified_cgroup_path(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    for line in cgroup.splitlines():
        hierarchy, _controllers, path = line.split(":", 2)
        if hierarchy == "0" and path:
            return path.strip()
    return None


def _process_parent_pid(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    try:
        parent = int(fields[1])
    except (IndexError, TypeError, ValueError):
        return None
    return parent if parent > 0 else None


def _has_same_cgroup_ancestor(pid: int, cgroup_path: str) -> bool:
    seen: set[int] = set()
    current = int(pid)
    while current > 1 and current not in seen:
        seen.add(current)
        parent = _process_parent_pid(current)
        if parent is None or parent <= 1:
            return False
        parent_path = _unified_cgroup_path(_process_cgroup(parent))
        if parent_path == cgroup_path:
            return True
        current = parent
    return False


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


def process_exit_requires_tmux_isolation(
    *,
    pid: int | None = None,
    timeout: float = 0.5,
) -> bool:
    """Return whether this process exiting can stop a group-killing service.

    A Gtk child inside a test runner or ordinary shell may exit without ending
    the containing service.  A production GUI is the service ``MainPID``; when
    that service uses ``KillMode=control-group`` or ``mixed``, its exit triggers
    cleanup of every remaining process in the unit.  Exact systemd properties
    are preferred.  If the user-manager bus is unavailable, a same-cgroup
    ancestor proves this process is not the service root; absence of one is
    treated conservatively as service ownership.
    """

    target = os.getpid() if pid is None else int(pid)
    cgroup = _process_cgroup(target)
    cgroup_path = _unified_cgroup_path(cgroup)
    unit = _unit_from_cgroup(cgroup)
    if not cgroup_path or not unit or not unit.endswith(".service"):
        return False

    properties = _unit_properties(unit, timeout=timeout)
    if _properties_bind_unit(properties, unit):
        try:
            main_pid = int(properties.get("MainPID", "0"))
        except (AttributeError, ValueError):
            main_pid = 0
        if main_pid > 0:
            if main_pid != target:
                return False
            return properties.get("KillMode", "control-group") not in {
                "process",
                "none",
            }

    return not _has_same_cgroup_ancestor(target, cgroup_path)


def isolate_existing_tmux_server(
    tmux_executable: str,
    *,
    query_timeout: float = 0.35,
    timeout: float = 2.0,
) -> TmuxServerIsolationResult:
    """Admit an isolated server or preserve it with a recovery-blocked result.

    No supported operation binds an existing process generation through the
    cgroup membership write. ``AttachProcessesToUnit`` accepts numeric PIDs.
    The ``PIDFDs`` transient-scope property, introduced in systemd 255, carries
    a generation over D-Bus, but the inspected systemd 262 implementation still
    calls ``pidref_verify`` followed by ``cg_attach`` with a numeric PID. Neither
    that interface nor a version check qualifies atomic legacy migration.
    """

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
            detail=(
                "recovery-blocked: user systemd is unavailable; "
                "the existing tmux server was left in its current cgroup"
            ),
        )

    return TmuxServerIsolationResult(
        ok=False,
        server_present=True,
        changed=False,
        server_pid=pid,
        server_start_ticks=start_ticks,
        unit=unit,
        cgroup=cgroup,
        detail=(
            "recovery-blocked: generation-bound migration of the existing tmux "
            "server is unavailable; the server was left in its current cgroup. "
            "Keep its owning service running until separately authorized "
            "lifecycle recovery preserves or retires its sessions"
        ),
    )
