from __future__ import annotations

import json
import os
import re
import shlex
import stat
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from .models import ResourceSnapshot

_PROC = "/proc"
_CLK_TCK = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# Prefer the fast /proc/<pid>/task/<tid>/children walk. Full /proc scans are only
# a fallback for kernels without that interface and are cached briefly.
_CHILDREN_CACHE_TTL = 0.75
_children_cache: tuple[float, dict[int, list[int]]] | None = None
_children_file_supported: bool | None = None

# Exact Codex recovery discovery is called from UI account switching.  Most
# pane trees contain many wrappers and helper processes but only one Codex
# process.  Cache the bounded rollout metadata by directory identity and the
# final process result by kernel start identity so repeated selector polls stay
# interactive without trusting a reused PID.
_CODEX_SESSION_CACHE_TTL = 2.0
_CODEX_ROLLOUT_DIRECTORY_CACHE_LIMIT = 32
_codex_session_cache_lock = threading.Lock()
_codex_session_cache: dict[tuple[str, int, int, str], tuple[float, str | None]] = {}
_codex_rollout_directory_cache: dict[
    str,
    tuple[int, int, float, tuple[tuple[str, str | None, float | None], ...]],
] = {}


def _clear_codex_session_caches() -> None:
    """Clear bounded recovery-discovery caches (primarily for deterministic tests)."""

    with _codex_session_cache_lock:
        _codex_session_cache.clear()
        _codex_rollout_directory_cache.clear()

# Automatic crash replay must stay narrower than ordinary user-entered shell
# commands. These flags do not carry prompts, paths, config values, tokens, or
# other user data. Value-bearing options and positional arguments are rejected
# as a whole rather than partially replayed with changed semantics.
_CODEX_RESTART_FLAGS = frozenset(
    {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--full-auto",
        "--no-alt-screen",
        "--oss",
        "--search",
        "--strict-config",
        "--yolo",
    }
)
_OPEN_CODE_MODEL_RE = re.compile(r"^(?:opencode|opencode-go)/[A-Za-z0-9._:+-]+$")
_OPEN_CODE_CONFIG_KEYS = frozenset(
    {
        "model_provider",
        "model_catalog_json",
        "model_providers.opencode_bridge.name",
        "model_providers.opencode_bridge.base_url",
        "model_providers.opencode_bridge.env_key",
        "model_providers.opencode_bridge.wire_api",
        "model_providers.opencode_bridge.requires_openai_auth",
    }
)


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def _process_argv(pid: int) -> tuple[str, ...] | None:
    """Read one bounded argv vector from proc without consulting shell text."""

    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as handle:
            data = handle.read(65537)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if not data or len(data) > 65536:
        return None
    return tuple(
        part.decode("utf-8", errors="replace")
        for part in data.rstrip(b"\0").split(b"\0")
        if part
    )


def _process_environment(pid: int) -> dict[str, str] | None:
    """Read one bounded process environment without exposing secret values."""

    try:
        with open(f"{_PROC}/{pid}/environ", "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if len(raw) > 1024 * 1024:
        return None
    environment: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        decoded_key = os.fsdecode(key)
        if decoded_key and decoded_key not in environment:
            environment[decoded_key] = os.fsdecode(value)
    return environment


def _unquote_config_value(value: str) -> str:
    clean = str(value)
    if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {"'", '"'}:
        return clean[1:-1]
    return clean


def _canonical_opencode_observation(
    argv: tuple[str, ...],
    environment: dict[str, str],
) -> tuple[str, ...] | None:
    """Recognize only the exact local codex-opencode provider decoration."""

    if not argv or Path(argv[0]).name.casefold() != "codex":
        return None
    if not environment.get("CODEX_OPENCODE_PROXY_KEY"):
        return None
    home = environment.get("HOME", "")
    if not home or not Path(home).is_absolute():
        return None
    state_home = environment.get("XDG_STATE_HOME") or str(Path(home) / ".local" / "state")
    expected_catalog = os.path.normpath(
        str(Path(state_home) / "codex-opencode" / "models.catalog.json")
    )

    configs: dict[str, str] = {}
    model: str | None = None
    launch_args: list[str] = []
    index = 1
    while index < len(argv):
        token = str(argv[index])
        if token in {"-c", "--config"}:
            if index + 1 >= len(argv):
                return None
            raw = str(argv[index + 1])
            if "=" not in raw:
                return None
            key, value = raw.split("=", 1)
            value = _unquote_config_value(value)
            if key == "sqlite_home":
                codex_home = environment.get("CODEX_HOME", "")
                if not codex_home or os.path.normpath(value) != os.path.normpath(codex_home):
                    return None
            elif key in _OPEN_CODE_CONFIG_KEYS and key not in configs:
                configs[key] = value
            else:
                return None
            index += 2
            continue
        if token in {"-m", "--model"}:
            if index + 1 >= len(argv) or model is not None:
                return None
            model = str(argv[index + 1])
            index += 2
            continue
        if token.startswith("--model="):
            if model is not None:
                return None
            model = token.partition("=")[2]
            index += 1
            continue
        if token == "--sandbox":
            if index + 1 >= len(argv) or argv[index + 1] != "workspace-write":
                return None
            launch_args.extend((token, "workspace-write"))
            index += 2
            continue
        if token == "--dangerously-bypass-hook-trust":
            index += 1
            continue
        if token in _CODEX_RESTART_FLAGS:
            launch_args.append(token)
            index += 1
            continue
        if token == "resume":
            if index + 1 >= len(argv) or index + 2 != len(argv):
                return None
            session_id = _canonical_codex_session_id(argv[index + 1])
            if session_id is None:
                return None
            launch_args.extend(("resume", session_id))
            index += 2
            continue
        return None

    if model is None or not _OPEN_CODE_MODEL_RE.fullmatch(model):
        return None
    if set(configs) != _OPEN_CODE_CONFIG_KEYS:
        return None
    if configs["model_provider"] != "opencode_bridge":
        return None
    if os.path.normpath(configs["model_catalog_json"]) != expected_catalog:
        return None
    if configs["model_providers.opencode_bridge.name"] != "OpenCode":
        return None
    if configs["model_providers.opencode_bridge.env_key"] != "CODEX_OPENCODE_PROXY_KEY":
        return None
    if configs["model_providers.opencode_bridge.wire_api"] != "responses":
        return None
    if configs["model_providers.opencode_bridge.requires_openai_auth"].casefold() != "false":
        return None
    try:
        endpoint = urlsplit(configs["model_providers.opencode_bridge.base_url"])
        endpoint_port = endpoint.port
    except ValueError:
        return None
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint_port is None
        or not 1 <= endpoint_port <= 65535
        or endpoint.path.rstrip("/") != "/v1"
        or endpoint.query
        or endpoint.fragment
    ):
        return None
    return ("codex-opencode", "-m", model, *launch_args)


def _managed_codex_observation_argv(
    pid: int,
    argv: tuple[str, ...],
) -> tuple[str, ...]:
    """Remove only verified TermDeck or codex-opencode launch decoration."""

    environment = _process_environment(pid)
    if environment is None:
        return argv
    opencode = _canonical_opencode_observation(argv, environment)
    if opencode is not None:
        return opencode

    if len(argv) < 3 or argv[1] not in {"-c", "--config"}:
        return argv
    codex_home = environment.get("CODEX_HOME", "")
    if not codex_home or not Path(codex_home).is_absolute():
        return argv
    raw_value = argv[2]
    if not raw_value.startswith("sqlite_home="):
        return argv
    configured = _unquote_config_value(raw_value.split("=", 1)[1])
    if os.path.normpath(configured) != os.path.normpath(codex_home):
        return argv
    cleaned = [argv[0], *argv[3:]]
    try:
        cleaned.remove("--dangerously-bypass-hook-trust")
    except ValueError:
        pass
    return tuple(cleaned)


def _process_cwd(pid: int) -> str | None:
    """Read one process cwd while it is still live and resolvable."""

    try:
        directory = Path(os.readlink(f"{_PROC}/{pid}/cwd"))
        if not directory.is_absolute() or not directory.is_dir():
            return None
        return str(directory.resolve())
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def _canonical_codex_session_id(value: object) -> str | None:
    """Return one canonical UUID, rejecting names and malformed identifiers."""

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _codex_rollout_metadata(path: Path) -> tuple[str, str | None, float | None] | None:
    """Read one bounded metadata record from an owner-only regular rollout."""

    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            return None
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="replace",
            closefd=True,
        ) as handle:
            descriptor = -1
            first_line = handle.readline(65537)
        if not first_line or len(first_line) > 65536:
            return None
        record = json.loads(first_line)
        payload = record.get("payload", {}) if isinstance(record, dict) else {}
        if not isinstance(payload, dict) or payload.get("source") != "cli":
            return None
        session_id = _canonical_codex_session_id(payload.get("id"))
        if session_id is None or not path.name.endswith(f"{session_id}.jsonl"):
            return None
        timestamp = payload.get("timestamp") or record.get("timestamp")
        observed_at: float | None = None
        if isinstance(timestamp, str) and timestamp.strip():
            clean = timestamp.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(clean)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            observed_at = parsed.timestamp()
        cwd = payload.get("cwd")
        return session_id, str(cwd) if isinstance(cwd, str) else None, observed_at
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _process_start_epoch(pid: int) -> float | None:
    stat = _parse_stat(pid)
    if not stat:
        return None
    boot = _read_text(f"{_PROC}/stat")
    if boot is None:
        return None
    boot_time: int | None = None
    for line in boot.splitlines():
        if line.startswith("btime "):
            try:
                boot_time = int(line.split()[1])
            except (IndexError, ValueError):
                return None
            break
    if boot_time is None:
        return None
    try:
        return float(boot_time) + (int(stat["starttime"]) / float(_CLK_TCK))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _cached_codex_rollout_metadata(
    directory: Path,
) -> tuple[tuple[str, str | None, float | None], ...]:
    """Return one bounded, short-lived regular-directory rollout index."""

    try:
        directory_metadata = directory.lstat()
    except OSError:
        return ()
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_uid != os.geteuid()
    ):
        return ()
    key = str(directory)
    now = time.monotonic()
    with _codex_session_cache_lock:
        cached = _codex_rollout_directory_cache.get(key)
        if (
            cached is not None
            and cached[:2]
            == (directory_metadata.st_mtime_ns, directory_metadata.st_ino)
            and now - cached[2] <= _CODEX_SESSION_CACHE_TTL
        ):
            return cached[3]
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        return ()
    if len(paths) > 4096:
        return ()
    rows = tuple(
        metadata
        for path in paths
        if (metadata := _codex_rollout_metadata(path)) is not None
    )
    with _codex_session_cache_lock:
        if len(_codex_rollout_directory_cache) >= _CODEX_ROLLOUT_DIRECTORY_CACHE_LIMIT:
            # Insertion order is deterministic on supported Python versions.
            _codex_rollout_directory_cache.pop(next(iter(_codex_rollout_directory_cache)), None)
        _codex_rollout_directory_cache[key] = (
            directory_metadata.st_mtime_ns,
            directory_metadata.st_ino,
            now,
            rows,
        )
    return rows


def _primary_codex_session_id(pid: int) -> str | None:
    """Find one process-bound primary Codex TUI session UUID.

    Open rollout descriptors are authoritative. Some Codex builds close that
    descriptor after startup, so a bounded fallback uses a directory metadata
    index shared by every pane poll and requires a unique CLI rollout with the
    same cwd and a creation timestamp within five minutes of the kernel process
    start.  Final results are cached only by PID *and* kernel start identity.
    """

    proc_cwd = _process_cwd(pid)
    candidates: list[tuple[int, int, str]] = []
    try:
        fd_names = os.listdir(f"{_PROC}/{pid}/fd")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        fd_names = []

    for fd_name in fd_names:
        try:
            target = Path(os.readlink(f"{_PROC}/{pid}/fd/{fd_name}"))
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if "/sessions/" not in str(target) or target.suffix != ".jsonl":
            continue
        metadata = _codex_rollout_metadata(target)
        if metadata is None:
            continue
        session_id, metadata_cwd, _created_at = metadata
        rank = 0 if proc_cwd and metadata_cwd == proc_cwd else 1
        try:
            modified_ns = target.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        candidates.append((rank, -modified_ns, session_id))

    if candidates:
        candidates.sort()
        return candidates[0][2]

    stat = _parse_stat(pid)
    try:
        start_ticks = int(stat["starttime"]) if stat else 0
    except (KeyError, TypeError, ValueError):
        start_ticks = 0
    if start_ticks <= 0 or not proc_cwd:
        return None
    cache_key = (str(_PROC), int(pid), start_ticks, proc_cwd)
    now = time.monotonic()
    with _codex_session_cache_lock:
        cached = _codex_session_cache.get(cache_key)
        if cached is not None and now - cached[0] <= _CODEX_SESSION_CACHE_TTL:
            return cached[1]

    environment = _process_environment(pid)
    started_at = _process_start_epoch(pid)
    result: str | None = None
    if environment is not None and started_at is not None:
        roots: list[Path] = []
        for raw_root in (
            environment.get("CODEX_HOME"),
            str(Path(environment.get("HOME", "")) / ".codex")
            if environment.get("HOME")
            else None,
        ):
            if not raw_root:
                continue
            root = Path(raw_root).expanduser()
            if not root.is_absolute():
                continue
            sessions = root / "sessions"
            try:
                identity = sessions.resolve(strict=False)
            except OSError:
                identity = sessions
            if identity not in roots:
                roots.append(identity)

        start_day = datetime.fromtimestamp(started_at, tz=timezone.utc)
        disk_candidates: list[tuple[float, str]] = []
        for root in roots:
            for day_offset in (-1, 0, 1):
                day = start_day + timedelta(days=day_offset)
                directory = root / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
                for session_id, metadata_cwd, created_at in _cached_codex_rollout_metadata(directory):
                    if metadata_cwd != proc_cwd or created_at is None:
                        continue
                    delta = abs(created_at - started_at)
                    if delta <= 300.0:
                        disk_candidates.append((delta, session_id))
        if disk_candidates:
            # Cwd and a five-minute start window are evidence, not identity.
            # Multiple distinct conversations inside that boundary are
            # underdetermined, so never guess by choosing the nearest timestamp.
            candidates = {session_id for _delta, session_id in disk_candidates}
            result = next(iter(candidates)) if len(candidates) == 1 else None

    with _codex_session_cache_lock:
        if len(_codex_session_cache) > 2048:
            cutoff = now - _CODEX_SESSION_CACHE_TTL
            for key, value in tuple(_codex_session_cache.items()):
                if value[0] < cutoff:
                    _codex_session_cache.pop(key, None)
        _codex_session_cache[cache_key] = (now, result)
    return result


def safe_codex_recovery_command(
    argv: Sequence[str],
    *,
    session_id: str | None = None,
) -> str | None:
    """Return one canonical restart-safe Codex/codex-opencode command."""

    if not argv:
        return None
    executable = Path(str(argv[0])).name.casefold()
    if executable not in {"codex", "codex-opencode"}:
        return None
    args = [str(part) for part in argv[1:]]
    command: list[str] = [executable]
    if executable == "codex-opencode":
        if len(args) < 2 or args[0] not in {"-m", "--model"}:
            return None
        model = args[1]
        if not _OPEN_CODE_MODEL_RE.fullmatch(model):
            return None
        command.extend(("-m", model))
        args = args[2:]

    resume_id: str | None = None
    if "resume" in args:
        resume_index = args.index("resume")
        launch_flags = args[:resume_index]
        resume_args = args[resume_index + 1 :]
        if len(resume_args) != 1:
            return None
        resume_id = _canonical_codex_session_id(resume_args[0])
        if resume_id is None:
            return None
    else:
        launch_flags = args

    normalized_flags: list[str] = []
    index = 0
    while index < len(launch_flags):
        flag = launch_flags[index]
        if flag == "--sandbox":
            if index + 1 >= len(launch_flags) or launch_flags[index + 1] != "workspace-write":
                return None
            normalized_flags.extend((flag, "workspace-write"))
            index += 2
            continue
        if flag not in _CODEX_RESTART_FLAGS:
            return None
        normalized_flags.append(flag)
        index += 1

    observed_id = _canonical_codex_session_id(session_id) if session_id else None
    if resume_id and observed_id and resume_id != observed_id:
        return None
    exact_id = resume_id or observed_id
    command.extend(normalized_flags)
    if exact_id:
        command.extend(("resume", exact_id))
    return shlex.join(command)


def safe_claude_recovery_command(argv: Sequence[str]) -> str | None:
    """Return one canonical exact Claude recovery command, or reject it.

    ``cc-go-ulw`` supplies credentials, model routing, and settings before it
    execs Claude Code.  Recovery must therefore retain that launcher while
    discarding path aliases and every option other than one exact session UUID.
    A bare launcher is a new conversation, not recovery state.
    """

    if not argv or str(argv[0]) != "cc-go-ulw":
        return None
    args = [str(part) for part in argv[1:]]
    if len(args) != 2 or args[0] != "--resume":
        return None
    raw_session_id = args[1]
    session_id = _canonical_codex_session_id(raw_session_id)
    if session_id is None or raw_session_id != session_id:
        return None
    return shlex.join(["cc-go-ulw", "--resume", session_id])


def _claude_process(argv: Sequence[str]) -> bool:
    """Recognize the configured wrapper or Claude's exec-replaced binary."""

    if not argv:
        return False
    executable = Path(str(argv[0]))
    if executable.name in {"cc-go-ulw", "claude", "claude-code"}:
        return True
    parts = executable.parts
    return len(parts) >= 3 and parts[-2] == "versions" and parts[-3] == "claude"


def _primary_claude_session_id(pid: int) -> str | None:
    """Read the primary Claude project JSONL held open by one live process.

    Primary conversations are root ``projects/<encoded-cwd>/<uuid>.jsonl``
    files.  Nested subagent/tool-result JSONLs are deliberately excluded.  The
    first bounded record must identify the same UUID as the filename; no prompt
    or transcript content is inspected.
    """

    proc_cwd = _process_cwd(pid)
    candidates: list[tuple[int, int, str]] = []
    try:
        fd_names = os.listdir(f"{_PROC}/{pid}/fd")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None

    for fd_name in fd_names:
        try:
            target = os.readlink(f"{_PROC}/{pid}/fd/{fd_name}")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        path = Path(target)
        if (
            path.suffix != ".jsonl"
            or path.parent.parent.name != "projects"
        ):
            continue
        session_id = _canonical_codex_session_id(path.stem)
        if session_id is None or path.stem != session_id:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline(65537)
            if not first_line or len(first_line) > 65536:
                continue
            payload = json.loads(first_line)
            if not isinstance(payload, dict):
                continue
            observed_id = _canonical_codex_session_id(payload.get("sessionId"))
            if observed_id != session_id:
                continue
            metadata_cwd = payload.get("cwd")
            rank = 0 if proc_cwd and metadata_cwd == proc_cwd else 1
            try:
                modified_ns = os.stat(path).st_mtime_ns
            except OSError:
                modified_ns = 0
            candidates.append((rank, -modified_ns, session_id))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def observe_recovery_metadata(
    root_pid: int,
    *,
    authority_callback=None,
) -> tuple[bool, str | None, str | None]:
    """Observe a safe resumable command and its cwd below a managed pane.

    The boolean distinguishes an idle, readable process tree from a vanished
    or unreadable one. Callers may clear a stale snapshot only for the former.
    Walking descendants handles terminal wrappers whose tmux pane process is
    not the shell or foreground TUI itself.
    """

    pids = process_tree(root_pid)
    if not pids:
        return False, None, None
    observed = False
    weaker_codex: tuple[str, str | None] | None = None
    for pid in pids:
        argv = _process_argv(pid)
        if argv is None:
            continue
        observed = True
        managed_argv = _managed_codex_observation_argv(pid, argv)
        managed_name = Path(managed_argv[0]).name.casefold() if managed_argv else ""
        session_id = (
            _primary_codex_session_id(pid)
            if managed_name in {"codex", "codex-opencode"}
            else None
        )
        command = safe_codex_recovery_command(
            managed_argv,
            session_id=session_id,
        )
        if command is not None:
            cwd = _process_cwd(pid)
            if session_id is not None:
                # Contained Codex deliberately enters a fresh session with
                # bubblewrap, so the native process has no host-side controlling
                # TTY. Its open primary rollout (source="cli") is stronger
                # identity evidence than host TTY ownership and already excludes
                # structured subagent rollouts.
                if authority_callback is not None and cwd is not None:
                    authority_callback(pid, command, cwd)
                return True, command, cwd
            stat = _parse_stat(pid)
            if not stat:
                continue
            # Weaker Node/containment launchers do not own the primary rollout.
            # Require host foreground-TTY ownership before retaining one as a
            # fallback so background launchers are never replayed automatically.
            if int(stat.get("tty_nr", 0)) == 0 or int(
                stat.get("pgrp", -1)
            ) != int(stat.get("tpgid", -2)):
                continue
            if weaker_codex is None:
                weaker_codex = (command, cwd)
            continue

        claude_session_id = _primary_claude_session_id(pid) if _claude_process(argv) else None
        if claude_session_id is not None:
            command = safe_claude_recovery_command(
                ("cc-go-ulw", "--resume", claude_session_id)
            )
        else:
            command = safe_claude_recovery_command(argv)
        if command is None:
            continue
        stat = _parse_stat(pid)
        if not stat:
            continue
        # cc-go-ulw is a direct terminal launcher, so exact Claude recovery is
        # retained only for the foreground TTY process.  This excludes detached
        # helpers which may briefly share the primary transcript descriptor.
        if int(stat.get("tty_nr", 0)) == 0 or int(stat.get("pgrp", -1)) != int(
            stat.get("tpgid", -2)
        ):
            continue
        return True, command, _process_cwd(pid)
    if weaker_codex is not None:
        return True, weaker_codex[0], weaker_codex[1]
    return observed, None, None


def observe_recovery_metadata_with_context(record, root_pid: int, *, native_observer=None):
    """Observe context at the same primary-native boundary; keep tuple callers."""
    from .recovery_authority_context import RecoveryAuthorityError, capture_codex_context
    context = None

    def capture(pid, command, cwd):
        nonlocal context
        context = capture_codex_context(record, pid, command, cwd, proc=_PROC, native_observer=native_observer)

    try:
        observed, command, cwd = observe_recovery_metadata(root_pid, authority_callback=capture)
    except RecoveryAuthorityError as exc:
        if str(exc) in {"native process generation changed during observation", "foreign process owner"}:
            # Positive reused/foreign owner evidence invalidates its transient
            # context while leaving the exact saved recipe untouched.
            return True, record.recovery_command, record.recovery_cwd, (None, None)
        return False, None, None, None
    except (OSError, ValueError, TypeError):
        # A raced process or source snapshot cannot strengthen its command.
        return False, None, None, None
    return observed, command, cwd, context


def observe_recovery_command(root_pid: int) -> tuple[bool, str | None]:
    """Compatibility wrapper returning only the observed recovery command."""

    observed, command, _cwd = observe_recovery_metadata(root_pid)
    return observed, command


def _parse_stat(pid: int) -> dict[str, int | str] | None:
    text = _read_text(f"{_PROC}/{pid}/stat")
    if not text or ")" not in text:
        return None
    try:
        before, rest_text = text.rsplit(")", 1)
        rest = rest_text.strip().split()
        if len(rest) < 20:
            return None
        return {
            "pid": int(before.split("(", 1)[0].strip()),
            "comm": before.split("(", 1)[1],
            "state": rest[0],
            "ppid": int(rest[1]),
            "pgrp": int(rest[2]),
            "tty_nr": int(rest[4]),
            "tpgid": int(rest[5]),
            "utime": int(rest[11]),
            "stime": int(rest[12]),
            "starttime": int(rest[19]),
        }
    except (IndexError, ValueError):
        return None


def _rss_bytes(pid: int) -> int:
    text = _read_text(f"{_PROC}/{pid}/statm")
    if not text:
        return 0
    try:
        resident_pages = int(text.split()[1])
        return resident_pages * _PAGE_SIZE
    except (IndexError, ValueError):
        return 0


def _total_jiffies() -> int:
    text = _read_text(f"{_PROC}/stat")
    if not text:
        return 0
    for line in text.splitlines():
        if line.startswith("cpu "):
            return sum(int(part) for part in line.split()[1:] if part.isdigit())
    return 0


def _boot_time() -> int:
    text = _read_text(f"{_PROC}/stat")
    if not text:
        return int(time.time())
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                break
    return int(time.time())


def _pid_alive(pid: int) -> bool:
    return os.path.exists(f"{_PROC}/{pid}")


def _direct_children_via_proc(pid: int) -> list[int] | None:
    """Return direct child PIDs using /proc task children files, or None if unsupported."""

    global _children_file_supported
    task_dir = f"{_PROC}/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return [] if _children_file_supported else None

    children: list[int] = []
    saw_children_file = False
    for tid in tids:
        path = f"{task_dir}/{tid}/children"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                data = handle.read().strip()
            saw_children_file = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if not data:
            continue
        for part in data.split():
            try:
                children.append(int(part))
            except ValueError:
                continue
    if saw_children_file:
        _children_file_supported = True
        return children
    if _children_file_supported is False:
        return None
    # First miss may mean the kernel lacks the interface.
    _children_file_supported = False
    return None


def _children_map_full_scan() -> dict[int, list[int]]:
    """Scan /proc once. Expensive — only used as fallback and cached briefly."""

    global _children_cache
    now = time.monotonic()
    if _children_cache is not None and (now - _children_cache[0]) < _CHILDREN_CACHE_TTL:
        return _children_cache[1]

    children: dict[int, list[int]] = defaultdict(list)
    try:
        with os.scandir(_PROC) as entries:
            for entry in entries:
                name = entry.name
                if not name.isdigit():
                    continue
                stat = _parse_stat(int(name))
                if stat:
                    children[int(stat["ppid"])].append(int(stat["pid"]))
    except OSError:
        pass
    mapping = dict(children)
    _children_cache = (now, mapping)
    return mapping


def process_tree(root_pid: int) -> list[int]:
    """Return root_pid and live descendants.

    Fast path walks /proc/<pid>/task/<tid>/children (O(descendants)).
    Fallback builds a full ppid map once and reuses it for ~750ms.

    Behaviour matches the original full-scan walker: missing/invalid roots
    yield ``[]``, and the root is included when it has a readable ``stat``.
    """

    if not root_pid or _parse_stat(root_pid) is None:
        return []

    # Fast path: walk only the descendant cone.
    first = _direct_children_via_proc(root_pid)
    if first is not None:
        seen: set[int] = set()
        order: list[int] = []
        queue: deque[int] = deque([root_pid])
        while queue:
            pid = queue.popleft()
            if pid in seen:
                continue
            # Keep the same liveness rule as the full scan: only include PIDs
            # that still have a readable /proc/<pid>/stat (except we already
            # validated the root above).
            if pid != root_pid and _parse_stat(pid) is None:
                continue
            seen.add(pid)
            order.append(pid)
            kids = _direct_children_via_proc(pid)
            if kids:
                queue.extend(kids)
        return order

    # Slow fallback for ancient kernels: one cached full scan.
    children = _children_map_full_scan()
    seen = set()
    order = []
    queue = deque([root_pid])
    while queue:
        pid = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        order.append(pid)
        queue.extend(children.get(pid, []))
    return order


def process_tree_contains_codex(root_pid: int) -> bool | None:
    """Return whether a readable process tree contains a Codex CLI process.

    ``None`` means the tree could not be inspected completely enough to prove
    absence. Callers that might destroy or recreate a session should treat
    that state as unknown and fail closed rather than assuming an idle shell.
    """

    pids = process_tree(root_pid)
    if not pids:
        return None
    unreadable = False
    for pid in pids:
        argv = _process_argv(pid)
        if argv is None:
            unreadable = True
            continue
        if not argv:
            continue
        executable = Path(str(argv[0])).name.casefold()
        if executable == "codex" or executable.startswith("codex-"):
            return True
        if executable in {"node", "nodejs"} and len(argv) > 1:
            if Path(str(argv[1])).name.casefold() == "codex":
                return True
    return None if unreadable else False


class ResourceSampler:
    """Linux /proc sampler with no third-party dependency.

    CPU percentage is calculated like top: 100% roughly means one full CPU core.
    """

    def __init__(
        self,
        *,
        snapshot_cache_ttl: float = 0.75,
        max_cache_entries: int = 4096,
    ) -> None:
        self._previous: dict[int, tuple[int, int]] = {}
        self._boot_time = _boot_time()
        self._cpu_count = max(1, os.cpu_count() or 1)
        self._jiffies_cache: tuple[float, int] | None = None
        self.snapshot_cache_ttl = max(0.0, float(snapshot_cache_ttl))
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._snapshot_cache: dict[int, tuple[float, ResourceSnapshot]] = {}
        self._bulk_refresh_cursor = 0

    def invalidate_cache(self, root_pid: int | None = None) -> None:
        """Drop cached resource snapshots after process-state changes."""
        if root_pid is None:
            self._snapshot_cache.clear()
            return
        self._snapshot_cache.pop(int(root_pid), None)

    def _cached_total_jiffies(self) -> int:
        now = time.monotonic()
        if self._jiffies_cache is not None and (now - self._jiffies_cache[0]) < 0.05:
            return self._jiffies_cache[1]
        value = _total_jiffies()
        self._jiffies_cache = (now, value)
        return value

    def _prune_caches(self) -> None:
        """Bound PID churn without evicting the whole working set at once."""

        while len(self._snapshot_cache) > self.max_cache_entries:
            oldest = next(iter(self._snapshot_cache))
            self._snapshot_cache.pop(oldest, None)
            self._previous.pop(oldest, None)
        while len(self._previous) > self.max_cache_entries:
            oldest = next(iter(self._previous))
            self._previous.pop(oldest, None)
            self._snapshot_cache.pop(oldest, None)

    def snapshot(self, root_pid: int | None) -> ResourceSnapshot:
        if not root_pid:
            return ResourceSnapshot(pid=None, exists=False)

        now = time.monotonic()
        if self.snapshot_cache_ttl:
            cached = self._snapshot_cache.get(int(root_pid))
            if cached is not None and now < cached[0]:
                self._snapshot_cache.pop(int(root_pid), None)
                self._snapshot_cache[int(root_pid)] = cached
                return cached[1]

        pids = process_tree(root_pid)
        if not pids:
            snap = ResourceSnapshot(pid=root_pid, exists=False)
            self._previous.pop(int(root_pid), None)
            if self.snapshot_cache_ttl:
                self._snapshot_cache.pop(int(root_pid), None)
                self._snapshot_cache[int(root_pid)] = (now + self.snapshot_cache_ttl, snap)
            self._prune_caches()
            return snap

        total_proc_jiffies = 0
        memory = 0
        state = "unknown"
        root_start = None

        for pid in pids:
            stat = _parse_stat(pid)
            if not stat:
                continue
            total_proc_jiffies += int(stat["utime"]) + int(stat["stime"])
            memory += _rss_bytes(pid)
            if pid == root_pid:
                state = str(stat["state"])
                root_start = int(stat["starttime"])

        total_jiffies = self._cached_total_jiffies()
        cpu_percent = 0.0
        # Reinsert to keep dict order as a tiny LRU for the bounded churn cache.
        previous = self._previous.pop(root_pid, None)
        if previous and total_jiffies > previous[1]:
            proc_delta = max(0, total_proc_jiffies - previous[0])
            total_delta = max(1, total_jiffies - previous[1])
            cpu_percent = (proc_delta / total_delta) * 100.0 * self._cpu_count
        self._previous[root_pid] = (total_proc_jiffies, total_jiffies)

        runtime = 0.0
        if root_start is not None:
            start_epoch = self._boot_time + (root_start / _CLK_TCK)
            runtime = max(0.0, time.time() - start_epoch)

        snap = ResourceSnapshot(
            pid=root_pid,
            exists=True,
            state=state,
            cpu_percent=cpu_percent,
            memory_bytes=memory,
            runtime_seconds=runtime,
            process_count=len(pids),
        )
        if self.snapshot_cache_ttl:
            self._snapshot_cache.pop(int(root_pid), None)
            self._snapshot_cache[int(root_pid)] = (now + self.snapshot_cache_ttl, snap)
        self._prune_caches()
        return snap

    def snapshots(
        self,
        root_pids: list[int | None],
        *,
        refresh_limit: int | None = None,
    ) -> list[ResourceSnapshot]:
        """Sample many roots with an optional round-robin refresh budget.

        A GTK refresh over thousands of agents must not synchronously walk every
        process tree. Fresh cache entries are always reused. At most
        ``refresh_limit`` stale/missing entries are sampled; the rest retain a
        stale snapshot or a lightweight "known by the mux, sample pending"
        placeholder until a later round.
        """

        if refresh_limit is None:
            return [self.snapshot(pid) for pid in root_pids]

        clean_limit = max(0, int(refresh_limit))
        now = time.monotonic()
        unique_pids = list(dict.fromkeys(int(pid) for pid in root_pids if pid))
        chosen: set[int] = set()
        if unique_pids and clean_limit:
            index = self._bulk_refresh_cursor % len(unique_pids)
            inspected = 0
            while inspected < len(unique_pids) and len(chosen) < clean_limit:
                pid = unique_pids[index]
                cached = self._snapshot_cache.get(pid)
                if cached is None or now >= cached[0]:
                    chosen.add(pid)
                index = (index + 1) % len(unique_pids)
                inspected += 1
            self._bulk_refresh_cursor = index

        refreshed = {pid: self.snapshot(pid) for pid in chosen}
        results: list[ResourceSnapshot] = []
        for root_pid in root_pids:
            if not root_pid:
                results.append(ResourceSnapshot(pid=None, exists=False))
                continue
            pid = int(root_pid)
            if pid in refreshed:
                results.append(refreshed[pid])
                continue
            cached = self._snapshot_cache.get(pid)
            if cached is not None:
                results.append(cached[1])
                continue
            # pane_pids supplied this PID, so represent it as live-but-pending
            # instead of falsely marking the remembered session missing.
            results.append(ResourceSnapshot(pid=pid, exists=True, state="unknown"))
        return results


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"
