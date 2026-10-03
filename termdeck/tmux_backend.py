from __future__ import annotations

import fcntl
import inspect
import os
import pwd
import re
import shlex
import shutil
import signal
import sqlite3
import stat as stat_module
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator, Sequence

from .checkpointing import CheckpointResult, DmtcpCheckpointManager
from .codex_auth import (
    CodexAuthError,
    CodexAuthState,
    CodexSwitchResult,
    canonical_codex_state_db_candidates,
    codex_state_backfill_complete,
    codex_state_migration_identity,
    ensure_private_codex_state,
    resolve_account,
)
from .codex_config_scope import (
    CodexConfigScopeError,
    migrate_quiescent_codex_configuration,
    prepare_private_bash_launch_command,
    validate_existing_private_bash_scope,
)
from .gateway_models import (
    GatewayModelStatus,
    gateway_model_status as classify_gateway_model_status,
    process_start_epoch,
)
from .history_control import (
    BackendHistoryState,
    ChildInputProtocol,
    EdgeKnowledge,
    HistoryOwner,
    ScrollDisposition,
    ScrollOutcome,
    passthrough_scroll,
    unhandled_scroll,
)
from .models import SessionRecord
from .session_recipe import (
    SessionRecipe,
    SessionRecipeError,
    automatic_recovery_allowed,
    build_session_recipe,
    encode_session_recipe,
    recipe_from_record,
    resolve_working_directory,
    should_restart_recipe,
)
from .resize_signal import (
    ResizeSignalResult,
    private_launch_tuple_for_scope,
    propagate_terminal_resize as _propagate_terminal_resize,
)
from .storage_fabric import StorageFabricError, tmux_subprocess_environment
from .resources import (
    _parse_stat,
    observe_recovery_metadata,
    process_tree,
    process_tree_contains_codex,
    safe_claude_recovery_command,
    safe_codex_recovery_command,
)
from .tmux_server_lifecycle import (
    TmuxServerIsolationError,
    TmuxServerIsolationResult,
    isolate_existing_tmux_server,
    scoped_tmux_server_command,
)


class TmuxError(RuntimeError):
    pass


class _AuthProcessesMayRemainStopped(TmuxError):
    """Internal receipt for a failed best-effort auth-process rollback."""

    def __init__(
        self,
        message: str,
        processes: tuple[tuple[int, int, bool], ...],
    ) -> None:
        super().__init__(message)
        self.processes = processes


class AuthTransactionBusy(TmuxError):
    """Another process is already changing this terminal's auth binding."""


_AUTH_TRANSACTION_LOCAL = threading.local()
_PRIVATE_AUTH_SCOPE_PATTERN = re.compile(r"auth-[A-Za-z0-9_-]+\Z")


def _auth_transaction_home(record: SessionRecord) -> Path:
    raw_home = (getattr(record, "codex_home", None) or "").strip()
    if not raw_home:
        raise TmuxError("terminal has no private Codex auth scope")
    candidate = Path(raw_home).expanduser()
    if not candidate.is_absolute():
        raise TmuxError("private Codex CODEX_HOME must be absolute")
    return Path(os.path.abspath(os.fspath(candidate)))


def _private_auth_scope_id(record: SessionRecord) -> str:
    scope_id = (getattr(record, "auth_scope_id", None) or "").strip()
    private_home = _auth_transaction_home(record)
    if (
        _PRIVATE_AUTH_SCOPE_PATTERN.fullmatch(scope_id) is None
        or private_home.name != scope_id
    ):
        raise TmuxError(
            "private Codex auth scope must match its CODEX_HOME basename and "
            "use the auth-[A-Za-z0-9_-]+ format"
        )
    return scope_id


def _auth_transaction_is_held(record: SessionRecord) -> bool:
    try:
        key = str(_auth_transaction_home(record))
    except TmuxError:
        return False
    return key in getattr(_AUTH_TRANSACTION_LOCAL, "held", set())


class _AuthLifetimeLease:
    """One idempotently releasable receipt for both transition lock paths."""

    def __init__(
        self,
        locks: tuple[tuple[int, Path], ...],
    ) -> None:
        self._locks = locks
        self._guard = threading.Lock()

    def release(self) -> None:
        with self._guard:
            locks = self._locks
            self._locks = ()
        for fd, _path in reversed(locks):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def held(self) -> bool:
        with self._guard:
            if not self._locks:
                return False
            return all(
                _auth_lifetime_lock_fd_is_safe(fd, path)
                for fd, path in self._locks
            )

    def matches(self, legacy_path: Path, canonical_path: Path) -> bool:
        with self._guard:
            expected = (legacy_path, canonical_path)
            return tuple(path for _fd, path in self._locks) == expected and all(
                _auth_lifetime_lock_fd_is_safe(fd, path)
                for fd, path in self._locks
            )

    def __del__(self) -> None:
        self.release()


def auth_lifetime_gate_root(record: SessionRecord | None = None) -> Path:
    """Resolve the exact gate root used by ``codex-contained-gates.sh``."""

    if record is not None:
        private_home = _auth_transaction_home(record)
        return private_home / ".termdeck-codex-lifetime"
    configured = os.environ.get("CODEX_TERMDECK_GATE_ROOT", "").strip()
    if configured:
        return Path(os.path.abspath(os.fspath(Path(configured).expanduser())))
    uid = os.geteuid()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
    if not runtime.is_dir():
        runtime = Path(os.environ.get("TMPDIR", "/tmp")) / f"codex-contained-runtime-{uid}"
    return Path(
        os.path.abspath(os.fspath((runtime / "codex-contained-startup").expanduser()))
    )


def _auth_lifetime_lock_path(record: SessionRecord) -> Path:
    scope_id = _private_auth_scope_id(record)
    return auth_lifetime_gate_root(record) / f"scope.{scope_id}.lock"


def _auth_legacy_lifetime_lock_path(record: SessionRecord) -> Path:
    """Return the persistent lock path held by pre-transition launchers."""

    scope_id = _private_auth_scope_id(record)
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    if configured:
        cache_home = Path(configured)
        if not cache_home.is_absolute():
            raise TmuxError("XDG_CACHE_HOME must be absolute for the legacy Codex lifetime gate")
    else:
        try:
            raw_user_home = pwd.getpwuid(os.geteuid()).pw_dir
        except (KeyError, OSError) as exc:
            raise TmuxError("could not resolve the user home for the legacy Codex lifetime gate") from exc
        cache_home = Path(raw_user_home) / ".cache"
    if not cache_home.is_absolute():
        raise TmuxError("user cache home must be absolute for the legacy Codex lifetime gate")
    root = Path(os.path.abspath(os.fspath(cache_home))) / "codex-contained-startup"
    return root / f"{scope_id}.lock"


@contextmanager
def auth_transaction_lock(record: SessionRecord):
    """Serialize one scope's pointer, process, and durable-store transaction.

    The lock file lives in the private scope rather than a global cache so CLI
    and GUI callers derive the same path without ambient configuration. It is
    deliberately non-blocking: a second picker gets an immediate retryable
    error instead of freezing behind a crashed or slow transaction.
    """

    _private_auth_scope_id(record)
    home = _auth_transaction_home(record)
    lock_key = str(home)
    held = getattr(_AUTH_TRANSACTION_LOCAL, "held", set())
    if lock_key in held:
        yield
        return
    try:
        home_metadata = home.lstat()
        if (
            not stat_module.S_ISDIR(home_metadata.st_mode)
            or home_metadata.st_uid != os.geteuid()
            or home_metadata.st_mode & 0o022
        ):
            raise AuthTransactionBusy("private Codex auth scope is unavailable")
        lock_path = home / ".termdeck-auth-transaction.lock"
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise AuthTransactionBusy(
                "this platform cannot open the auth transaction lock safely"
            )
        fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | nofollow,
            0o600,
        )
    except AuthTransactionBusy:
        raise
    except OSError as exc:
        raise AuthTransactionBusy(f"private Codex auth transaction lock is unavailable: {exc}") from exc
    try:
        try:
            metadata = os.fstat(fd)
            path_metadata = lock_path.stat(follow_symlinks=False)
            if (
                not stat_module.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                raise AuthTransactionBusy("private Codex auth transaction lock has unsafe ownership or mode")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path_metadata = lock_path.stat(follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise AuthTransactionBusy("private Codex auth transaction lock changed while opening")
        except BlockingIOError as exc:
            raise AuthTransactionBusy(
                "another Codex account change is already running for this terminal"
            ) from exc
        except OSError as exc:
            raise AuthTransactionBusy(f"could not lock this terminal's Codex account: {exc}") from exc
        _AUTH_TRANSACTION_LOCAL.held = {*held, lock_key}
        try:
            yield
        finally:
            _AUTH_TRANSACTION_LOCAL.held = held
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _auth_lifetime_lock_fd_is_safe(fd: int, path: Path) -> bool:
    try:
        metadata = os.fstat(fd)
        path_metadata = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat_module.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
        and not metadata.st_mode & 0o022
        and stat_module.S_ISREG(path_metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino)
        == (path_metadata.st_dev, path_metadata.st_ino)
    )


def _ensure_auth_lifetime_root(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_metadata = root.lstat()
    except OSError as exc:
        raise TmuxError(f"Codex scope lifetime gate is unavailable: {exc}") from exc
    if (
        root.is_symlink()
        or not stat_module.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat_module.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise TmuxError(
            f"Codex scope lifetime gate is unsafe (expected owner/mode {os.geteuid()}:700): {root}"
        )


def _open_auth_lifetime_lock(path: Path) -> int:
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise TmuxError("this platform cannot open the Codex scope lifetime lock safely")
        fd = os.open(
            path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | nofollow,
            0o600,
        )
    except TmuxError:
        raise
    except OSError as exc:
        raise TmuxError(f"Codex scope lifetime lock is unavailable: {exc}") from exc
    try:
        if not _auth_lifetime_lock_fd_is_safe(fd, path):
            raise TmuxError(f"Codex scope lifetime lock has unsafe ownership or mode: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TmuxError(
                "another Codex process still owns this terminal's auth scope; "
                "the account pointer was not changed"
            ) from exc
        except OSError as exc:
            raise TmuxError(f"could not lock this terminal's Codex auth scope: {exc}") from exc
        if not _auth_lifetime_lock_fd_is_safe(fd, path):
            raise TmuxError(f"Codex scope lifetime lock changed while opening: {path}")
        return fd
    except Exception:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _acquire_auth_lifetime_lease(record: SessionRecord) -> _AuthLifetimeLease:
    """Join both generations of launcher scope lock without waiting.

    The transaction has already stopped the bound process tree before this is
    called.  A busy lease therefore proves that another scope owner still
    exists outside that tree, and pointer mutation must fail closed.
    """

    paths = (
        _auth_legacy_lifetime_lock_path(record),
        _auth_lifetime_lock_path(record),
    )
    locks: list[tuple[int, Path]] = []
    try:
        for path in paths:
            _ensure_auth_lifetime_root(path.parent)
            locks.append((_open_auth_lifetime_lock(path), path))
        lease = _AuthLifetimeLease(tuple(locks))
        if not lease.matches(*paths):
            lease.release()
            locks.clear()
            raise TmuxError("Codex scope lifetime locks changed during acquisition")
        return lease
    except Exception:
        for fd, _path in reversed(locks):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        raise


@dataclass(frozen=True)
class AuthActivationResult:
    """Result of making a private auth-file change effective in a live tab."""

    ok: bool
    restarted: bool
    recovery_command: str | None = None
    recovery_cwd: str | None = None
    pid: int | None = None
    status: str | None = None
    detail: str = ""
    preserve_selected_account: bool = False
    command: str | None = None
    cwd: str | None = None
    clear_recovery: bool = False
    credential_lifecycle: str = "unobserved"


@dataclass(frozen=True)
class AuthActivationPlan:
    """Immutable preflight evidence for one private-auth activation.

    Live plans bind the tmux session and pane identity observed before the
    account file is changed. ``quiesce_auth_change`` rechecks that identity
    and stops the bound writer before a caller may change the account pointer.
    """

    ok: bool
    live: bool = False
    restart: bool = False
    environment_ready: bool = False
    session_created: str | None = None
    pane_id: str | None = None
    pane_pid: int | None = None
    pane_start_time: int | None = None
    pane_current_command: str | None = None
    pane_current_path: str | None = None
    replacement_command: str | None = None
    replacement_cwd: str | None = None
    recovery_command: str | None = None
    recovery_cwd: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class AuthQuiesceResult:
    """Receipt proving the old private-auth consumer cannot write.

    Restart plans remove the bound tmux session only after revalidating its
    exact recovery identity.  Live panes have tmux input disabled before their
    process tree is inspected or stopped so a terminal key cannot race the
    account transaction.  Callers must present this receipt to
    ``apply_auth_change``; changing the credential pointer without it is
    deliberately unsupported.
    """

    ok: bool
    live: bool = False
    terminated: bool = False
    suspended: bool = False
    plan_token: tuple[object, ...] = ()
    suspended_processes: tuple[tuple[int, int, bool], ...] = ()
    input_pane_id: str | None = None
    input_was_off: bool = False
    gated_respawn: bool = False
    recovered_payload: bool = False
    current_pid: int | None = None
    gate_path: str | None = None
    replacement_record: SessionRecord | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    launch_spec: _LaunchSpec | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    detail: str = ""
    lifetime_lease: _AuthLifetimeLease | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def release_lifetime_lease(self) -> None:
        if self.lifetime_lease is not None:
            self.lifetime_lease.release()

    @property
    def lifetime_lease_ready(self) -> bool:
        return self.lifetime_lease is not None and self.lifetime_lease.held


@dataclass(frozen=True)
class AuthTransactionResult:
    """End-to-end account switch receipt shared by GTK and the CLI."""

    ok: bool
    switch_result: CodexSwitchResult | None = None
    activation: AuthActivationResult | None = None
    error: str = ""
    rolled_back: bool = False


@dataclass(frozen=True)
class _AuthPaneIdentity:
    session_created: str
    pane_id: str
    pane_pid: int
    current_command: str
    current_path: str


@dataclass(frozen=True)
class _PaneLifecycle:
    pane_id: str
    dead: bool
    exit_status: int | None
    current_path: str
    # Captured by the same tmux display-message as pane_id/dead/path. Keeping
    # this optional preserves historical dead-pane fixtures while live callers
    # can reject cached or cross-generation PID evidence.
    pane_pid: int | None = None


@dataclass(frozen=True)
class DeadPayloadRecoveryResult:
    """One authoritative in-place response to a dead managed pane."""

    action: str
    pid: int
    command: str | None = None
    cwd: str | None = None
    clear_recovery: bool = False
    detail: str = ""


@dataclass(frozen=True)
class _LaunchSpec:
    recipe: SessionRecipe
    command: str
    directory: Path
    environment: tuple[tuple[str, str], ...]
    checkpoint: bool = False
    application_native_resume: bool = False
    confined_resume_permit: str | None = None


class TmuxBackend:
    """Hidden tmux backend.

    The GUI calls these methods; users never see or type tmux commands.
    """

    _READ_ONLY_CONTROL_COMMANDS = frozenset(
        {
            "capture-pane",
            "display-message",
            "has-session",
            "list-panes",
            "list-sessions",
            "show-environment",
        }
    )
    _SUPPORTED_IDLE_SHELLS = frozenset(
        {
            "bash",
            "csh",
            "dash",
            "elvish",
            "fish",
            "ksh",
            "nu",
            "sh",
            "tcsh",
            "zsh",
        }
    )

    def __init__(
        self,
        executable: str = "tmux",
        *,
        pane_cache_ttl: float = 1.0,
        command_timeout: float = 2.0,
        query_timeout: float = 0.35,
        failure_backoff: float = 1.0,
        history_limit: int = 2000,
        max_capture_lines: int = 10000,
        recovery_scan_limit: int = 128,
        checkpoint_manager: DmtcpCheckpointManager | None = None,
    ) -> None:
        self.executable = executable
        self.pane_cache_ttl = max(0.0, float(pane_cache_ttl))
        self.command_timeout = max(0.1, float(command_timeout))
        self.query_timeout = max(0.05, float(query_timeout))
        self.failure_backoff = max(0.05, float(failure_backoff))
        self.history_limit = max(100, int(history_limit))
        self.max_capture_lines = max(100, int(max_capture_lines))
        self.recovery_scan_limit = max(1, int(recovery_scan_limit))
        self.checkpoints = checkpoint_manager or DmtcpCheckpointManager()
        self._recovery_scan_cursor = 0
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target: dict[str, int] = {}
        self._pane_cache_by_session_active: dict[str, int] = {}
        self._pane_cwd_by_target: dict[str, str] = {}
        self._pane_cwd_by_session_active: dict[str, str] = {}
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0
        self._interactive_config_retry_at = 0.0
        self._query_backoff_until = 0.0
        self._scroll_views: dict[str, BackendHistoryState] = {}
        self._history_map_lock = threading.RLock()
        self._history_session_locks: dict[str, threading.RLock] = {}
        self._history_owner_validation_ttl = 0.75

    def invalidate_cache(self) -> None:
        """Drop cached tmux pane metadata after structural changes."""
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target = {}
        self._pane_cache_by_session_active = {}
        self._pane_cwd_by_target = {}
        self._pane_cwd_by_session_active = {}
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0

    def _query_timeout_for(self, item_count: int) -> float:
        """Give large fleet snapshots enough time without allowing hangs."""

        count = max(0, int(item_count))
        extra = max(0, count - 64) * 0.00075
        ceiling = max(self.query_timeout, self.command_timeout)
        return min(ceiling, self.query_timeout + extra)

    @property
    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    @property
    def pane_snapshot_stale(self) -> bool:
        """Whether pane PID results are last-known data after a failed query."""

        return self._pane_snapshot_stale

    def require_available(self) -> None:
        if not self.available:
            raise TmuxError("tmux is not installed or not found on PATH")

    def _run(
        self,
        args: Sequence[str],
        check: bool = True,
        *,
        timeout: float | None = None,
        bypass_query_backoff: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.require_available()
        try:
            child_environment = tmux_subprocess_environment(materialize=True)
        except StorageFabricError as exc:
            raise TmuxError(str(exc)) from exc
        command = [self.executable, *args]
        limit = self.command_timeout if timeout is None else max(0.05, float(timeout))
        command_name = str(args[0]) if args else ""
        starts_server = command_name in {"new-session", "start-server"}
        execution_command = command
        if starts_server:
            existing = None
            try:
                existing = isolate_existing_tmux_server(
                    self.executable,
                    query_timeout=self.query_timeout,
                    timeout=self.command_timeout,
                )
            except TmuxServerIsolationError:
                # This is only a fast-path probe.  Preserve the primary tmux
                # command's own timeout/error contract, then require exact
                # lifecycle isolation after any successful server start.
                pass
            if existing is None or not existing.server_present:
                execution_command = scoped_tmux_server_command(command) or command
        read_only_control = command_name in self._READ_ONLY_CONTROL_COMMANDS
        if (
            read_only_control
            and not bypass_query_backoff
            and time.monotonic() < self._query_backoff_until
        ):
            detail = f"tmux query suppressed during control-plane backoff: {command_name or 'command'}"
            if check:
                raise TmuxError(detail)
            return subprocess.CompletedProcess(command, 124, "", detail)
        try:
            result = subprocess.run(
                execution_command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=limit,
                env=child_environment,
            )
        except subprocess.TimeoutExpired as exc:
            detail = f"tmux command timed out after {limit:g}s: {args[0] if args else 'command'}"
            if read_only_control:
                self._query_backoff_until = time.monotonic() + self.failure_backoff
            if check:
                raise TmuxError(detail) from exc
            return subprocess.CompletedProcess(command, 124, "", detail)
        if check and result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux command failed")
        if result.returncode == 0 and starts_server:
            try:
                isolate_existing_tmux_server(
                    self.executable,
                    query_timeout=self.query_timeout,
                    timeout=self.command_timeout,
                )
            except TmuxServerIsolationError as exc:
                raise TmuxError(str(exc)) from exc
        return result

    def ensure_server_lifecycle_isolated(self) -> TmuxServerIsolationResult:
        """Keep an existing tmux server outside the GUI service cgroup."""

        self.require_available()
        try:
            return isolate_existing_tmux_server(
                self.executable,
                query_timeout=self.query_timeout,
                timeout=self.command_timeout,
            )
        except TmuxServerIsolationError as exc:
            raise TmuxError(str(exc)) from exc

    def session_exists(self, record: SessionRecord) -> bool:
        if not self.available:
            return False
        result = self._run(
            ["has-session", "-t", record.tmux_name],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode == 124:
            # A transiently busy tmux server is not proof that a remembered
            # running session disappeared. Preserve the prior live state and
            # let the next bounded query repair it.
            return record.status.casefold() in {"running", "paused", "missing"}
        return result.returncode == 0

    def _auth_session_exists(self, record: SessionRecord) -> bool:
        """Return exact liveness for credential transactions or fail closed."""

        if not self.available:
            raise TmuxError("tmux is unavailable while checking the terminal auth writer")
        result = self._run(
            ["has-session", "-t", record.tmux_name],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode == 124:
            # Credential-writer admission remains fail closed, but one busy
            # control-plane interval is not proof of presence or absence.
            # Retry exactly once with the existing command timeout and bypass
            # only the backoff created by the first bounded timeout.
            result = self._run(
                ["has-session", "-t", record.tmux_name],
                check=False,
                timeout=self.command_timeout,
                bypass_query_backoff=True,
            )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise TmuxError(
            result.stderr.strip()
            or "tmux could not prove whether the terminal auth writer is running"
        )

    @contextmanager
    def auth_scope_writer_quiescence(
        self,
        record: SessionRecord,
    ) -> Iterator[dict[str, object]]:
        """Hold exact per-scope fencing while a missing tab is rebound.

        The transaction lock excludes another TermDeck mutation, while the two
        non-blocking lifetime locks cover both legacy and current managed Codex
        launchers.  The caller must keep this context open through the durable
        auth-scope compare-and-swap so an active writer cannot enter between
        the quiescence observation and the rebind.
        """

        with auth_transaction_lock(record):
            if self._auth_session_exists(record):
                yield {
                    "quiescent": False,
                    "detail": "the terminal auth session is still live",
                }
                return
            lifetime_lease = _acquire_auth_lifetime_lease(record)
            try:
                if not lifetime_lease.held:
                    raise TmuxError(
                        "Codex scope lifetime lease changed during quiescence proof"
                    )
                if self._auth_session_exists(record):
                    yield {
                        "quiescent": False,
                        "detail": "the terminal auth session became live during proof",
                    }
                    return
                yield {
                    "quiescent": True,
                    "detail": "both Codex lifetime generations are exclusively fenced",
                }
            finally:
                lifetime_lease.release()

    def private_auth_environment_ready(self, record: SessionRecord) -> bool:
        """Verify the bound pane process was born with this tab's auth scope.

        Assigning ``codex_home`` to an old SQLite row cannot change an already
        running shell's environment. Account switching must fail closed for
        those legacy sessions, otherwise ``codex-auth`` can resolve to the
        global executable and mutate another tab's credentials.  Tmux's mutable
        session environment is not evidence about an already-running process,
        so the lifecycle binds this check to ``/proc/<pane-pid>/environ`` and a
        stable Linux process start time.
        """

        if getattr(record, "external", False) or not self.available:
            return False
        try:
            pane = self._inspect_auth_pane(record)
        except (OSError, TmuxError):
            return False
        if pane is None:
            return False
        identity = self._auth_process_identity(pane.pane_pid)
        if identity is None:
            return False
        return (
            self._private_auth_process_environment_ready(
                record,
                pane.pane_pid,
                identity[1],
            )
            or self._private_managed_bash_environment_ready(
                record,
                pane.pane_pid,
                identity[1],
            )
            or self._private_auth_process_tree_environment_ready(
                record,
                pane.pane_pid,
            )
        )

    @classmethod
    def _private_auth_process_tree_environment_ready(
        cls,
        record: SessionRecord,
        root_pid: int,
    ) -> bool:
        """Accept a recovery only when one live descendant owns this scope.

        Exact TUI recovery is normally launched through a shell, an isolation
        launcher, and bubblewrap.  The pane shell can retain its old startup
        PATH while the absolute private wrapper gives the actual Codex writer
        the correct environment.  Bind readiness to that writer rather than
        falsely rejecting a proven exact descendant because of its ancestor.
        """

        for pid in process_tree(root_pid):
            identity = cls._auth_process_identity(pid)
            if identity is not None and cls._private_auth_process_environment_ready(
                record,
                pid,
                identity[1],
            ):
                return True
        return False

    @classmethod
    def _private_auth_process_tree_scope_ready(
        cls,
        record: SessionRecord,
        root_pid: int,
    ) -> bool:
        """Verify the gated handoff shell inherited the exact private scope.

        Before the owner-only gate opens, the placeholder is an absolute
        ``/bin/bash --noprofile --norc`` process rather than the generated
        private interactive shell or Codex writer.  It cannot consume the
        credential yet, so the safe readiness contract is the exact
        CODEX_HOME/scope/lifetime-gate tuple; private-bin precedence is checked
        again on the resumed payload after the gate opens.
        """

        for pid in process_tree(root_pid):
            identity = cls._auth_process_identity(pid)
            if identity is not None and cls._private_auth_process_environment_ready(
                record,
                pid,
                identity[1],
                require_private_path=False,
            ):
                return True
        return False

    @staticmethod
    def _private_auth_path(codex_home: Path, *, base_path: str | None = None) -> str:
        """Prepend this scope without discarding ordinary host PATH entries."""

        expected_bin = str(codex_home / "bin")
        entries = [expected_bin]
        source = os.environ.get("PATH", os.defpath) if base_path is None else base_path
        for raw_entry in source.split(os.pathsep):
            normalized = os.path.normpath(raw_entry)
            if normalized == os.path.normpath(expected_bin):
                continue
            candidate = Path(raw_entry)
            if (
                candidate.name == "bin"
                and candidate.parent.name.startswith("auth-")
                and candidate.parent.parent.name == "terminal-auth"
            ):
                # A host process launched from another TermDeck tab commonly
                # carries that tab's private bin first. Never let it become a
                # fallback for this terminal.
                continue
            entries.append(raw_entry)
        return os.pathsep.join(entries)

    @classmethod
    def _private_auth_process_environment_ready(
        cls,
        record: SessionRecord,
        pid: int,
        expected_start_time: int,
        *,
        require_private_path: bool = True,
    ) -> bool:
        """Read a bounded, PID-reuse-safe environment from the live process."""

        raw_home = (getattr(record, "codex_home", None) or "").strip()
        scope_id = (getattr(record, "auth_scope_id", None) or "").strip()
        if not raw_home or not scope_id:
            return False
        expected_home = str(Path(raw_home).expanduser().resolve(strict=False))
        expected_bin = str(Path(expected_home) / "bin")
        expected_gate_root = str(auth_lifetime_gate_root(record))
        before = cls._auth_process_identity(pid)
        if before is None or before[1] != expected_start_time:
            return False
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                raw_environment = handle.read(1024 * 1024 + 1)
        except (FileNotFoundError, OSError):
            return False
        if len(raw_environment) > 1024 * 1024:
            return False
        environment: dict[str, str] = {}
        for raw_entry in raw_environment.split(b"\0"):
            if not raw_entry or b"=" not in raw_entry:
                continue
            raw_key, raw_value = raw_entry.split(b"=", 1)
            try:
                key = os.fsdecode(raw_key)
                value = os.fsdecode(raw_value)
            except (TypeError, UnicodeError):
                return False
            environment[key] = value
        after = cls._auth_process_identity(pid)
        if after is None or after[1] != expected_start_time:
            return False
        path_entries = environment.get("PATH", "").split(os.pathsep)
        scope_ready = (
            environment.get("CODEX_HOME") == expected_home
            and environment.get("TERMDECK_AUTH_SCOPE_ID") == scope_id
            and environment.get("CODEX_TERMDECK_GATE_ROOT") == expected_gate_root
        )
        return scope_ready and (
            not require_private_path
            or (bool(path_entries) and path_entries[0] == expected_bin)
        )

    @classmethod
    def _private_managed_bash_environment_ready(
        cls,
        record: SessionRecord,
        pid: int,
        expected_start_time: int,
    ) -> bool:
        """Verify the managed Bash boundary when ``/proc/environ`` is stale.

        Bash startup files can replace an inherited PATH.  Linux exposes the
        process's original environment area through ``/proc/<pid>/environ``;
        later exported strings may live elsewhere and are not reliably
        observable there.  The managed rc sources the user rc and reasserts
        private-bin precedence last, so bind this exception to the exact
        generated Bash argv and a secure marker-bearing rc file.  Codex
        descendants themselves still require the strict PATH check above.
        """

        if not cls._private_auth_process_environment_ready(
            record,
            pid,
            expected_start_time,
            require_private_path=False,
        ):
            return False
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        if not raw_home:
            return False
        home = Path(raw_home).expanduser().resolve(strict=False)
        rc_path = home / "bin" / ".termdeck-bashrc"
        before = cls._auth_process_identity(pid)
        if before is None or before[1] != expected_start_time:
            return False
        try:
            raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if len(raw_cmdline) > 64 * 1024:
                return False
            argv = [os.fsdecode(part) for part in raw_cmdline.split(b"\0") if part]
            shell = Path(argv[0]).resolve(strict=True) if argv else None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            rc_fd = os.open(rc_path, flags)
            try:
                metadata = os.fstat(rc_fd)
                if (
                    not stat_module.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or metadata.st_mode & 0o022
                ):
                    return False
                with os.fdopen(rc_fd, "r", encoding="utf-8") as handle:
                    rc_fd = -1
                    rc_head = handle.read(4096)
            finally:
                if rc_fd >= 0:
                    os.close(rc_fd)
        except (FileNotFoundError, OSError, RuntimeError, UnicodeError):
            return False
        after = cls._auth_process_identity(pid)
        return (
            after is not None
            and after[1] == expected_start_time
            and shell is not None
            and shell.name.casefold() == "bash"
            and argv[1:] == ["--noprofile", "--rcfile", str(rc_path), "-i"]
            and rc_head.startswith("# termdeck-private-bash-rc-v1\n")
            and f"export CODEX_HOME={shlex.quote(str(home))}\n" in rc_head
            and (
                f"export CODEX_TERMDECK_GATE_ROOT="
                f"{shlex.quote(str(home / '.termdeck-codex-lifetime'))}\n"
            ) in rc_head
            and f"export PATH={shlex.quote(str(home / 'bin'))}:" in rc_head
        )

    @staticmethod
    def _server_is_absent(result: subprocess.CompletedProcess[str]) -> bool:
        detail = str(result.stderr or "").casefold()
        return "no server running on" in detail or (
            ("no such file or directory" in detail or "connection refused" in detail)
            and ("error connecting to" in detail or "connect failed" in detail)
        )

    def session_exists_many(self, records: Sequence[SessionRecord]) -> dict[str, bool]:
        """Resolve many sessions with one bounded tmux query."""

        out = {record.id: False for record in records}
        if not records or not self.available:
            return out
        result = self._run(
            ["list-sessions", "-F", "#{session_name}"],
            check=False,
            timeout=self._query_timeout_for(len(records)),
        )
        if result.returncode != 0:
            if self._server_is_absent(result):
                # This is the normal post-reboot state, not a busy control
                # plane. Report the sessions missing so startup recovery can
                # relaunch every remembered managed tab from durable records.
                return out
            # A failed control-plane snapshot is not proof that thousands of
            # independent sessions died. Keep the last known lifecycle state;
            # a later successful bounded query will reconcile it.
            return {
                record.id: record.status.casefold() in {"running", "paused", "missing"}
                for record in records
            }
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return {record.id: record.tmux_name in names for record in records}

    def reconciliation_snapshot(self, records: Sequence[SessionRecord]) -> dict[str, dict[str, object]]:
        """Fresh liveness evidence; neither registry PIDs nor the UI cache are authority.

        In the server's PID namespace, bind the pane to its tmux parent and
        kernel start time. An inaccessible namespace remains unobservable,
        rather than borrowing the identity of an unrelated local PID.
        """
        result = self._run([
            "list-panes", "-a", "-F",
            "#{pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}",
        ], check=False, timeout=self._query_timeout_for(len(records)))
        if result.returncode:
            if self._server_is_absent(result):
                return {record.id: {"state": "missing", "pid": None} for record in records}
            raise TmuxError("tmux reconciliation snapshot is unavailable")
        panes: dict[str, dict[str, object]] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 7 or parts[6] not in {"0", "1"}:
                raise TmuxError("tmux reconciliation snapshot is malformed")
            server, session, window, pane, pane_id, pid, dead = parts
            try:
                server_pid, pane_pid = int(server), int(pid)
            except ValueError as exc:
                raise TmuxError("tmux reconciliation process identity is malformed") from exc
            if min(server_pid, pane_pid) <= 1 or not pane_id.startswith("%"):
                raise TmuxError("tmux reconciliation process identity is invalid")
            evidence: dict[str, object] = {"pid": pane_pid, "server_pid": server_pid, "state": "dead"}
            if dead == "0":
                try:
                    same_namespace = os.path.samefile(
                        f"/proc/{server_pid}/exe", shutil.which(self.executable) or self.executable,
                    )
                except OSError:
                    same_namespace = False
                if not same_namespace:
                    evidence["state"] = "unobservable"
                else:
                    process = _parse_stat(pane_pid)
                    if process and process["state"] not in {"Z", "X", "x"}:
                        if process["ppid"] == server_pid:
                            evidence.update(state="live", start_time=process["starttime"])
                        else:
                            evidence["state"] = "identity-mismatch"
            panes[f"{session}:{window}.{pane}"] = evidence
            panes[pane_id] = evidence
            panes.setdefault(session, evidence)
        out = {}
        for record in records:
            target = self.pane_target(record)
            evidence = panes.get(target)
            if evidence is None and target == f"{record.tmux_name}:0.0":
                evidence = panes.get(record.tmux_name)
            out[record.id] = dict(evidence or {"state": "missing", "pid": None})
        return out

    @staticmethod
    def reconciliation_identity_current(evidence: dict[str, object]) -> bool:
        """Reject exit or PID reuse between the tmux query and registry update."""
        process = _parse_stat(int(evidence["pid"]))
        return bool(process and process["state"] not in {"Z", "X", "x"}
                    and process["ppid"] == evidence.get("server_pid")
                    and process["starttime"] == evidence.get("start_time"))

    @staticmethod
    def _resume_uuid(command: str) -> str | None:
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if "resume" not in parts:
            return None
        index = parts.index("resume")
        if index + 2 != len(parts):
            return None
        candidate = parts[index + 1].strip()
        return candidate or None

    @staticmethod
    def _state_db_columns(connection: sqlite3.Connection, table: str) -> list[str]:
        return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]

    @staticmethod
    def _canonical_state_db_candidates(private_db: Path) -> list[Path]:
        return canonical_codex_state_db_candidates(private_db)

    @staticmethod
    def _state_db_migration_identity(
        connection: sqlite3.Connection,
    ) -> tuple[tuple[int, bytes], ...] | None:
        return codex_state_migration_identity(connection)

    @staticmethod
    def _state_db_backfill_complete(connection: sqlite3.Connection) -> bool:
        return codex_state_backfill_complete(connection)

    @staticmethod
    def _readonly_state_db(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _recovery_rollout_path(
        private_home: Path,
        source_row: sqlite3.Row,
    ) -> Path:
        try:
            rollout = Path(str(source_row["rollout_path"])).expanduser().resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise TmuxError("Codex recovery thread has an unavailable rollout path") from exc
        if not rollout.is_file():
            raise TmuxError("Codex recovery rollout path is not a regular file")

        archived_value = source_row["archived"] if "archived" in source_row.keys() else 0
        if type(archived_value) is not int or archived_value not in (0, 1):
            raise TmuxError("Codex recovery thread has an invalid archived marker")
        archived = archived_value == 1
        root_name = "archived_sessions" if archived else "sessions"
        configured_root = private_home / root_name
        try:
            shared_root = configured_root.resolve(strict=True)
        except OSError as exc:
            raise TmuxError(
                f"Codex recovery requires an accessible private {root_name} root"
            ) from exc
        if not shared_root.is_dir():
            raise TmuxError(f"Codex recovery {root_name} root is not a directory")
        try:
            rollout.relative_to(shared_root)
        except ValueError as exc:
            raise TmuxError(
                "Codex recovery rollout lies outside the private shared-history root"
            ) from exc
        return rollout

    def _hydrate_private_codex_recovery(self, record: SessionRecord, recovery: str) -> None:
        """Hydrate one exact thread row into a private Codex state database.

        Per-terminal auth homes intentionally keep SQLite state separate to avoid
        multi-terminal lock contention. Exact recovery still needs the target
        thread metadata, so copy only that row (plus direct FK/tool metadata)
        from a canonical state DB immediately before launching ``codex resume``.
        """

        raw_home = (getattr(record, "codex_home", None) or "").strip()
        thread_id = self._resume_uuid(recovery)
        if not raw_home or not thread_id:
            return
        private_home = Path(raw_home).expanduser().resolve(strict=False)
        private_db = private_home / "state_5.sqlite"
        if private_db.is_symlink() or not private_db.is_file():
            raise TmuxError(
                "exact Codex recovery requires a private, initialized state database"
            )
        source_candidates = self._canonical_state_db_candidates(private_db)
        try:
            if private_db.stat().st_nlink != 1:
                raise TmuxError(
                    "exact Codex recovery refuses a hard-linked private state database"
                )
        except OSError as exc:
            raise TmuxError("private Codex state database cannot be inspected") from exc
        for candidate in source_candidates:
            try:
                aliases_canonical = candidate.is_file() and os.path.samefile(
                    private_db, candidate
                )
            except OSError:
                aliases_canonical = False
            if aliases_canonical:
                raise TmuxError(
                    "exact Codex recovery refuses a private state database that aliases canonical state"
                )

        try:
            local = sqlite3.connect(private_db, timeout=5.0)
        except sqlite3.Error as exc:
            raise TmuxError(f"could not open private Codex state database for recovery: {exc}") from exc
        local.row_factory = sqlite3.Row
        try:
            if "threads" not in {
                str(row[0])
                for row in local.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }:
                raise TmuxError("private Codex state database has no threads table")
            local_migrations = self._state_db_migration_identity(local)
            if local_migrations is None:
                raise TmuxError(
                    "private Codex state database has no complete SQLx migration ledger"
                )
            if not self._state_db_backfill_complete(local):
                raise TmuxError(
                    "private Codex state backfill is not complete; refusing to "
                    "modify possibly active native backfill state"
                )
            if local.execute("SELECT 1 FROM threads WHERE id=? LIMIT 1", (thread_id,)).fetchone():
                return
            local_thread_columns = self._state_db_columns(local, "threads")

            source_connection: sqlite3.Connection | None = None
            source_row: sqlite3.Row | None = None
            source_path: Path | None = None
            invalid_rollout_error: TmuxError | None = None
            for candidate in source_candidates:
                if not candidate.is_file():
                    continue
                try:
                    source = self._readonly_state_db(candidate)
                except (sqlite3.Error, ValueError):
                    continue
                try:
                    source.execute("BEGIN")
                except sqlite3.Error:
                    source.close()
                    continue
                try:
                    if (
                        self._state_db_migration_identity(source) != local_migrations
                        or not self._state_db_backfill_complete(source)
                        or self._state_db_columns(source, "threads") != local_thread_columns
                    ):
                        source.close()
                        continue
                    row = source.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
                    if row is None:
                        source.close()
                        continue
                    try:
                        self._recovery_rollout_path(private_home, row)
                    except TmuxError as exc:
                        invalid_rollout_error = exc
                        source.close()
                        continue
                    source_connection = source
                    source_row = row
                    source_path = candidate
                    break
                except sqlite3.Error:
                    source.close()
                    continue

            if source_connection is None or source_row is None:
                if invalid_rollout_error is not None:
                    raise invalid_rollout_error
                raise TmuxError(
                    "exact Codex session metadata is absent from the private "
                    f"and canonical state databases: {thread_id}"
                )

            try:
                local.execute("PRAGMA foreign_keys=ON")
                local.execute("BEGIN IMMEDIATE")
                if not self._state_db_backfill_complete(local):
                    raise TmuxError(
                        "private Codex state backfill changed during exact recovery"
                    )
                if (
                    self._state_db_migration_identity(local) != local_migrations
                    or self._state_db_columns(local, "threads") != local_thread_columns
                ):
                    raise TmuxError(
                        "private Codex state schema changed during exact recovery"
                    )
                if local.execute(
                    "SELECT 1 FROM threads WHERE id=? LIMIT 1",
                    (thread_id,),
                ).fetchone():
                    local.commit()
                    return
                section_id = source_row["thread_section_id"] if "thread_section_id" in source_row.keys() else None
                if section_id:
                    source_section_columns = self._state_db_columns(source_connection, "thread_sections")
                    local_section_columns = self._state_db_columns(local, "thread_sections")
                    if source_section_columns != local_section_columns:
                        raise TmuxError("Codex thread_sections schema mismatch during exact recovery")
                    section = source_connection.execute(
                        "SELECT * FROM thread_sections WHERE id=?", (section_id,)
                    ).fetchone()
                    if section is None:
                        raise TmuxError("Codex recovery thread references a missing thread section")
                    quoted = ",".join(f'"{column}"' for column in local_section_columns)
                    placeholders = ",".join("?" for _ in local_section_columns)
                    existing_section = local.execute(
                        "SELECT * FROM thread_sections WHERE id=?", (section_id,)
                    ).fetchone()
                    section_values = [section[column] for column in local_section_columns]
                    if existing_section is None:
                        local.execute(
                            f"INSERT INTO thread_sections ({quoted}) VALUES ({placeholders})",
                            section_values,
                        )
                    elif [existing_section[column] for column in local_section_columns] != section_values:
                        raise TmuxError(
                            "Codex recovery thread section conflicts with private state"
                        )

                quoted = ",".join(f'"{column}"' for column in local_thread_columns)
                placeholders = ",".join("?" for _ in local_thread_columns)
                local.execute(
                    f"INSERT INTO threads ({quoted}) VALUES ({placeholders})",
                    [source_row[column] for column in local_thread_columns],
                )

                source_tool_columns = self._state_db_columns(source_connection, "thread_dynamic_tools")
                local_tool_columns = self._state_db_columns(local, "thread_dynamic_tools")
                if source_tool_columns != local_tool_columns:
                    raise TmuxError(
                        "Codex thread_dynamic_tools schema mismatch during exact recovery"
                    )
                if source_tool_columns:
                    tools = source_connection.execute(
                        "SELECT * FROM thread_dynamic_tools WHERE thread_id=? ORDER BY position",
                        (thread_id,),
                    ).fetchall()
                    if tools:
                        quoted_tools = ",".join(f'"{column}"' for column in local_tool_columns)
                        placeholders_tools = ",".join("?" for _ in local_tool_columns)
                        local.executemany(
                            "INSERT INTO thread_dynamic_tools "
                            f"({quoted_tools}) VALUES ({placeholders_tools})",
                            ([tool[column] for column in local_tool_columns] for tool in tools),
                        )
                if local.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise TmuxError(
                        "Codex exact recovery metadata failed foreign-key validation"
                    )
                local.commit()
            except Exception:
                local.rollback()
                raise
            finally:
                source_connection.close()

            if not local.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
                raise TmuxError(
                    f"Codex recovery metadata hydration did not persist thread {thread_id} from {source_path}"
                )
        except TmuxError:
            raise
        except sqlite3.Error as exc:
            raise TmuxError(f"could not hydrate exact Codex recovery metadata: {exc}") from exc
        finally:
            local.close()

    @staticmethod
    def _validated_initial_grid(columns: int, rows: int) -> tuple[int, int]:
        """Return a safe tmux grid for a payload's very first terminal frame.

        Full-screen TUIs render immediately after ``tmux new-session``. If
        tmux starts them at its implicit 80x24 default and the GUI resizes the
        pane a moment later, their splash screen is briefly composed from two
        incompatible layouts. The GUI therefore supplies its settled VTE grid
        before starting a missing payload. Headless callers keep the historical
        ``create_or_resume`` path and tmux default.
        """

        try:
            safe_columns = int(columns)
            safe_rows = int(rows)
        except (TypeError, ValueError) as exc:
            raise TmuxError("initial terminal grid must contain integers") from exc
        if isinstance(columns, bool) or isinstance(rows, bool):
            raise TmuxError("initial terminal grid must contain integers")
        if safe_columns < 2 or safe_rows < 2:
            raise TmuxError("initial terminal grid must be at least 2x2")
        # tmux stores dimensions in bounded integers; an explicit ceiling also
        # prevents a corrupted GUI allocation from becoming an enormous pane.
        if safe_columns > 1000 or safe_rows > 1000:
            raise TmuxError("initial terminal grid is unreasonably large")
        return safe_columns, safe_rows

    def _pane_lifecycle(self, record: SessionRecord) -> _PaneLifecycle | None:
        """Return one authoritative pane lifecycle snapshot, or unknown.

        A timeout or malformed response is not proof that a payload exited.
        Callers therefore leave the pane untouched unless tmux explicitly says
        ``pane_dead=1`` for the durable managed target.
        """

        result = self._run(
            [
                "display-message",
                "-p",
                "-t",
                self.pane_target(record),
                "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}\t#{pane_current_path}",
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.rstrip("\n").split("\t", 4)
        if len(parts) != 5 or not parts[0].startswith("%"):
            return None
        dead_text = parts[1].strip()
        if dead_text not in {"0", "1"}:
            return None
        status_text = parts[2].strip()
        exit_status: int | None = None
        if status_text:
            try:
                exit_status = int(status_text)
            except ValueError:
                return None
        pid_text = parts[3].strip()
        pane_pid: int | None = None
        if pid_text:
            try:
                candidate_pid = int(pid_text)
            except ValueError:
                return None
            if candidate_pid > 1:
                pane_pid = candidate_pid
            elif dead_text == "0":
                # A live pane without a safe process identity is not an
                # authoritative live payload snapshot.
                return None
        elif dead_text == "0":
            return None
        return _PaneLifecycle(
            pane_id=parts[0],
            dead=dead_text == "1",
            exit_status=exit_status,
            current_path=self._normalise_pane_directory(parts[4].strip()) or "",
            pane_pid=pane_pid,
        )

    def authoritative_live_pane_pid(self, record: SessionRecord) -> int | None:
        """Return a live pane PID from one uncached tmux lifecycle snapshot.

        Registry repair must never treat ``last_pid`` or the short-lived pane
        cache as proof that a red/missing row is actually live. Missing, dead,
        malformed, and temporarily unqueryable targets all return ``None``.
        """

        lifecycle = self._pane_lifecycle(record)
        if lifecycle is None or lifecycle.dead:
            return None
        pid = lifecycle.pane_pid
        return pid if pid is not None and pid > 1 else None

    @staticmethod
    def _durable_recipe(record: SessionRecord) -> SessionRecipe:
        try:
            recipe = recipe_from_record(record)
        except SessionRecipeError as exc:
            raise TmuxError(f"durable session recipe is invalid: {exc}") from exc
        if recipe is None:
            if getattr(record, "external", False):
                raise TmuxError(
                    "adopted external tmux session has no explicit managed recovery recipe"
                )
            raise TmuxError("managed terminal has no durable session recipe")
        return recipe

    @staticmethod
    def _environment_args(environment: Sequence[tuple[str, str]]) -> list[str]:
        args: list[str] = []
        for key, value in environment:
            args.extend(["-e", f"{key}={value}"])
        return args

    @staticmethod
    def _with_explicit_private_environment(
        command: str,
        environment: Sequence[tuple[str, str]],
        *,
        login_shell: str | None = None,
    ) -> str:
        """Start a private-scope payload with its actual inherited environment.

        tmux stores a session environment PATH but does not consistently pass
        a replacement PATH to a respawned process. The private auth contract
        is process based, so make the four scope values explicit at exec time.
        env replaces itself with the supervisor shell, leaving proc and every
        descendant bound to the scope.
        """

        values = dict(environment)
        assignments = [
            f"{name}={values[name]}"
            for name in (
                "CODEX_HOME",
                "TERMDECK_AUTH_SCOPE_ID",
                "CODEX_TERMDECK_GATE_ROOT",
                "PATH",
            )
            if name in values
        ]
        if login_shell is not None:
            # Configured host runtimes need the same profile/tool environment
            # as an ordinary login terminal. Their authenticated entrypoint
            # reapplies the private scope after user startup files, before it
            # execs the selected runtime. A failing entrypoint stays failed.
            return shlex.join(
                ["/usr/bin/env", *assignments, login_shell, "-ilc", command]
            )
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        shell_argv = bool(argv) and (
            Path(argv[0]).name.casefold() == "bash"
            and "-c" not in argv
            and not any(
                any(character in item for character in "|&;<>()$" + chr(96))
                for item in argv
            )
        )
        if shell_argv:
            return shlex.join(["/usr/bin/env", *assignments, *argv])
        return shlex.join(
            [
                "/usr/bin/env",
                *assignments,
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                command,
            ]
        )

    @classmethod
    def _command_is_interactive_shell(cls, command: str) -> bool:
        """Return whether *command* already leaves the pane at a shell prompt.

        A direct shell (optionally with login/interactive flags) is already the
        durable terminal payload. A shell executing ``-c`` is only a launcher
        and must still receive the same post-application fallback as any other
        command. Keeping this test syntax-based avoids naming any agent or TUI.
        """

        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        if not argv:
            return False
        raw_executable = argv[0]
        if os.path.sep in raw_executable and not raw_executable.startswith((os.path.sep, "./", "../")):
            # Reject wrapper syntax such as ``wrapped(/bin/bash)``. A shell is
            # direct only when argv[0] itself is a basename or path.
            return False
        executable = Path(raw_executable).name.casefold().removeprefix("-")
        if executable not in cls._SUPPORTED_IDLE_SHELLS:
            return False
        for token in argv[1:]:
            if token in {"-c", "--command"}:
                return False
            # Shells accept short-option clusters such as ``-lc``. Long
            # options like ``--norc`` are not command-string launchers.
            if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
                return False
        return True

    @classmethod
    def _command_is_direct_bash_shell(cls, command: str) -> bool:
        """Return whether *command* is a direct interactive Bash invocation.

        Private-account migration must not compare raw path spellings: on
        merged-/usr systems ``/bin/bash`` and ``/usr/bin/bash`` are the same
        executable, while a durable recipe may preserve either spelling.  A
        direct Bash prompt is safe to replace with Termdeck's private rcfile;
        Bash command-string launchers remain application payloads.
        """

        if not cls._command_is_interactive_shell(command):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return bool(argv) and Path(argv[0]).name.casefold().removeprefix("-") == "bash"

    @classmethod
    def _fallback_shell_command(cls, record: SessionRecord) -> str:
        """Resolve an interactive shell without trusting nested tmux state."""

        original = str(getattr(record, "command", "") or "").strip()
        if cls._command_is_interactive_shell(original):
            return original

        candidates = [os.environ.get("SHELL", "")]
        try:
            candidates.append(pwd.getpwuid(os.geteuid()).pw_shell)
        except (KeyError, OSError):
            pass
        candidates.extend(["/bin/bash", "/bin/sh"])
        for candidate in candidates:
            raw = str(candidate or "").strip()
            if not raw:
                continue
            resolved = shutil.which(raw) if os.path.sep not in raw else raw
            if not resolved:
                continue
            path = Path(resolved)
            if path.name.casefold().removeprefix("-") not in cls._SUPPORTED_IDLE_SHELLS:
                continue
            try:
                path = path.resolve(strict=True)
            except OSError:
                continue
            if path.is_file() and os.access(path, os.X_OK):
                return shlex.join([str(path), "-i"])
        return "/bin/sh -i"

    @staticmethod
    def _with_interactive_shell_fallback(command: str, shell_command: str) -> str:
        """Keep a successful/interactive application exit in the same pane.

        The payload runs in a subshell so an application's own ``exec`` or
        ``exit`` cannot replace the supervisor. Successful exits and an
        operator Ctrl-C (130) return to an interactive shell. Other non-zero
        exits remain real pane failures, preserving ``on-failure`` restart
        semantics. Explicit Termdeck Kill removes the tmux session itself and
        therefore cannot be intercepted by this wrapper.
        """

        return (
            "(\n"
            f"{command}\n"
            ")\n"
            "__termdeck_status=$?\n"
            "if [ \"$__termdeck_status\" -eq 0 ] || [ \"$__termdeck_status\" -eq 130 ]; then\n"
            "  printf '\\r\\n[T1000 application exited; interactive shell ready]\\r\\n' >&2\n"
            f"  exec {shell_command}\n"
            "fi\n"
            "exit \"$__termdeck_status\""
        )

    def _launch_spec(self, record: SessionRecord) -> _LaunchSpec:
        """Resolve a launch without requiring any external agent or tool.

        Application-native resume commands and validated process checkpoints
        remain higher-fidelity optional inputs. Otherwise the canonical recipe
        is authoritative and is replayed as opaque user-owned shell syntax.
        """

        recipe = self._durable_recipe(record)
        try:
            resolved = resolve_working_directory(recipe)
        except SessionRecipeError as exc:
            raise TmuxError(str(exc)) from exc
        if record.tag == "web-agent":
            # A legacy or revoked configured plan must be refused before user
            # login profiles run with the newly authorized host permissions.
            from .agent_launch import validate_native_launch
            try:
                validate_native_launch(record)
            except (OSError, ValueError) as exc:
                raise TmuxError(str(exc)) from exc
        # A long-lived tmux server retains its first caller's environment.
        # New and recovered tabs use the current host caller's identity/tool
        # environment, with explicit durable recipe choices still authoritative.
        environment = {
            key: os.environ[key]
            for key in ("HOME", "USER", "LOGNAME", "SHELL", "PATH")
            if key in os.environ
        }
        environment.update(recipe.environment_dict())
        environment.update(resolved.environment)
        launch_directory = resolved.path
        command = recipe.command
        fallback_shell_command = self._fallback_shell_command(record)
        checkpoint = False
        native_resume = False

        raw_codex_home = (getattr(record, "codex_home", None) or "").strip()
        if record.tag == "web-agent" and raw_codex_home:
            # A configured task keeps its authenticated launch plan through
            # every account restart. The entrypoint owns native resume and
            # private scope restoration after normal shell startup.
            from .agent_launch import prepare_resume
            resume_permit = None
            try:
                if record.recovery_command:
                    command, resume_permit = prepare_resume(record)
                    native_resume = True
            except (OSError, ValueError) as exc:
                raise TmuxError(str(exc)) from exc
            codex_home = Path(raw_codex_home).expanduser().resolve(strict=False)
            environment.update({"CODEX_HOME": str(codex_home),
                "TERMDECK_AUTH_SCOPE_ID": record.auth_scope_id or "",
                "CODEX_TERMDECK_GATE_ROOT": str(auth_lifetime_gate_root(record)),
                "PATH": self._private_auth_path(codex_home, base_path=environment.get("PATH"))})
            command = self._with_explicit_private_environment(
                command, environment.items(),
                login_shell=shlex.split(fallback_shell_command)[0],
            )
            return _LaunchSpec(recipe=recipe, command=command, directory=launch_directory,
                               environment=tuple(sorted(environment.items())), checkpoint=False,
                               application_native_resume=native_resume,
                               confined_resume_permit=resume_permit)
        # Native recognizers are an optional private-account adapter. Generic
        # records never consult agent-specific syntax: their canonical recipe is
        # opaque application-owned shell input.
        codex_recovery = (
            self._exact_codex_recovery(record) if raw_codex_home else None
        )
        if codex_recovery:
            native_resume = True
            command = codex_recovery
            raw_recovery_cwd = (getattr(record, "recovery_cwd", None) or "").strip()
            if raw_recovery_cwd:
                native_recipe = SessionRecipe(
                    command=codex_recovery,
                    cwd=str(Path(raw_recovery_cwd).expanduser().resolve(strict=False)),
                    restart_policy=recipe.restart_policy,
                    origin="native-resume",
                )
                native_directory = resolve_working_directory(native_recipe)
                launch_directory = native_directory.path
                environment.update(native_directory.environment)
        else:
            checkpoint_command = self.checkpoints.restart_command(record)
            if checkpoint_command:
                checkpoint = True
                command = checkpoint_command
                launch_directory = self.checkpoints.session_dir(record)
            elif (
                recipe.origin in {"launch", "legacy-launch"}
                and not self._is_codex_launch_command(recipe.command)
            ):
                # Legacy Codex sessions remain excluded from CRIU wrapping, but
                # this is only a checkpoint compatibility optimization. Generic
                # recipe replay neither needs nor invokes a Codex executable.
                command = self.checkpoints.wrap_launch_command(record, recipe.command)
            else:
                native_resume = recipe.origin in {"legacy-recovery", "native-resume"}

        if raw_codex_home:
            # A saved direct Bash shell has no Codex conversation to hydrate.
            # Preserve its already managed scope; do not require (or qualify)
            # a Codex executable merely to restore the terminal itself.
            direct_bash = (
                not checkpoint
                and not native_resume
                and not codex_recovery
                and recipe.origin in {"launch", "legacy-launch"}
                and command == recipe.command
                and self._saved_direct_bash_command(record, command)
            )
            if direct_bash:
                try:
                    validate_existing_private_bash_scope(
                        raw_codex_home, auth_scope_id=record.auth_scope_id or ""
                    )
                except (CodexConfigScopeError, OSError, RuntimeError) as exc:
                    raise TmuxError(f"saved private Bash scope requires repair: {exc}") from exc
                fallback_shell_command = command
            else:
                try:
                    migrate_quiescent_codex_configuration(raw_codex_home)
                except CodexConfigScopeError as exc:
                    raise TmuxError(
                        f"could not migrate stopped private Codex configuration: {exc}"
                    ) from exc
                try:
                    ensure_private_codex_state(raw_codex_home, require_complete=True)
                except (CodexAuthError, CodexConfigScopeError) as exc:
                    raise TmuxError(str(exc)) from exc
            if codex_recovery:
                self._hydrate_private_codex_recovery(record, codex_recovery)
            codex_home = Path(raw_codex_home).expanduser().resolve(strict=False)
            isolated_path = self._private_auth_path(codex_home, base_path=environment.get("PATH"))
            try:
                fallback_argv = shlex.split(fallback_shell_command)
            except ValueError:
                fallback_argv = []
            private_shell_command: str | None = None
            if (
                fallback_argv
                and Path(fallback_argv[0]).name.casefold() == "bash"
            ):
                try:
                    private_shell_command = prepare_private_bash_launch_command(
                        codex_home,
                        auth_scope_id=(getattr(record, "auth_scope_id", "") or ""),
                        shell_path=fallback_argv[0],
                        require_existing_scope=direct_bash,
                    )
                except CodexConfigScopeError as exc:
                    raise TmuxError(
                        f"private Bash environment could not be prepared: {exc}"
                    ) from exc
            if private_shell_command is not None:
                fallback_shell_command = private_shell_command
            if codex_recovery:
                private_recovery = self._private_exact_codex_launch_command(
                    record,
                    codex_recovery,
                    isolated_path,
                )
                suffix = command[len(codex_recovery) :]
                command = private_recovery + suffix
            elif (
                private_shell_command is not None
                and self._command_is_direct_bash_shell(command)
            ):
                command = private_shell_command
            environment.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "TERMDECK_AUTH_SCOPE_ID": (
                        getattr(record, "auth_scope_id", "") or ""
                    ),
                    "CODEX_TERMDECK_GATE_ROOT": str(auth_lifetime_gate_root(record)),
                    "PATH": isolated_path,
                }
            )

        # Configured tasks retain their authenticated entrypoint and its exit
        # status; a rejected plan must not turn into an implicit shell launch.
        if record.tag != "web-agent" and not self._command_is_interactive_shell(command):
            command = self._with_interactive_shell_fallback(
                command,
                fallback_shell_command,
            )

        if record.tag == "web-agent":
            command = self._with_explicit_private_environment(
                command, environment.items(),
                login_shell=shlex.split(fallback_shell_command)[0],
            )
        elif raw_codex_home:
            command = self._with_explicit_private_environment(
                command,
                environment.items(),
            )

        return _LaunchSpec(
            recipe=recipe,
            command=command,
            directory=launch_directory,
            environment=tuple(sorted(environment.items())),
            checkpoint=checkpoint,
            application_native_resume=native_resume,
        )

    def _respawn_dead_pane(
        self,
        record: SessionRecord,
        lifecycle: _PaneLifecycle,
        *,
        lifetime_lease: _AuthLifetimeLease | None = None,
    ) -> bool:
        if not lifecycle.dead:
            return False
        recipe = self._durable_recipe(record)
        status = record.status.casefold()
        explicitly_starting = status == "new"
        if status in {"stopped", "archived"}:
            return False
        if not explicitly_starting and not should_restart_recipe(
            recipe,
            session_missing=False,
            exit_status=lifecycle.exit_status,
        ):
            return False
        spec = self._launch_spec(record)
        # Private launch wrappers acquire the same lifetime locks. The exited
        # pane has no live writer; release immediately before atomic respawn
        # while retaining the auth transaction lock around this mutation.
        if lifetime_lease is not None:
            lifetime_lease.release()
        args = ["respawn-pane", "-t", lifecycle.pane_id]
        args.extend(self._environment_args(spec.environment))
        args.extend(["-c", str(spec.directory), spec.command])
        self._run(args)
        self._run(
            ["set-option", "-t", record.tmux_name, "remain-on-exit", "on"],
            check=False,
        )
        self.invalidate_cache()
        return True

    def _await_live_pane_pid(
        self,
        record: SessionRecord,
        *,
        pane_id: str,
        timeout: float = 10.0,
    ) -> int:
        """Wait until the same tmux pane owns one live replacement process."""

        deadline = time.monotonic() + max(0.05, float(timeout))
        last_detail = "replacement pane was not ready"
        while True:
            result = self._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    self.pane_target(record),
                    "#{pane_id}\t#{pane_pid}\t#{pane_dead}",
                ],
                check=False,
                timeout=self.query_timeout,
            )
            if result.returncode == 0:
                parts = result.stdout.rstrip("\n").split("\t", 2)
                if len(parts) == 3:
                    observed_pane, pid_text, dead_text = parts
                    if observed_pane != pane_id:
                        raise TmuxError(
                            "replacement changed the durable tmux pane identity"
                        )
                    if dead_text.strip() in {"", "0"}:
                        try:
                            pid = int(pid_text)
                        except ValueError:
                            last_detail = "replacement pane reported an invalid pid"
                        else:
                            if pid > 1:
                                self.invalidate_cache()
                                return pid
                    else:
                        last_detail = "replacement pane is still dead"
                else:
                    last_detail = "replacement pane returned malformed identity"
            else:
                last_detail = result.stderr.strip() or "replacement pane query failed"
            if time.monotonic() >= deadline:
                raise TmuxError(last_detail)
            time.sleep(0.025)

    def _respawn_clean_exit_shell(
        self,
        record: SessionRecord,
        lifecycle: _PaneLifecycle,
        *,
        lifetime_lease: _AuthLifetimeLease | None = None,
    ) -> DeadPayloadRecoveryResult:
        """Bridge one pre-supervisor successful payload exit to a durable shell."""

        if not lifecycle.dead or lifecycle.exit_status not in {0, 130}:
            raise TmuxError("clean-exit shell bridge requires a dead successful pane")
        fallback_command = self._fallback_shell_command(record)
        requested_cwd = lifecycle.current_path or record.cwd
        recipe = build_session_recipe(
            fallback_command,
            requested_cwd,
            restart_policy="on-failure",
            origin="native-resume",
        )
        encoded, digest = encode_session_recipe(recipe)
        shell_record = replace(
            record,
            command=fallback_command,
            cwd=recipe.cwd,
            status="new",
            recovery_command=None,
            recovery_cwd=None,
            recovery_recipe=encoded,
            recovery_recipe_sha256=digest,
            restart_policy="on-failure",
        )
        spec = self._launch_spec(shell_record)
        if lifetime_lease is not None:
            lifetime_lease.release()
        args = ["respawn-pane", "-t", lifecycle.pane_id]
        args.extend(self._environment_args(spec.environment))
        args.extend(["-c", str(spec.directory), spec.command])
        self._run(args)
        self._run(
            ["set-option", "-t", record.tmux_name, "remain-on-exit", "on"],
            check=False,
        )
        pid = self._await_live_pane_pid(record, pane_id=lifecycle.pane_id)
        return DeadPayloadRecoveryResult(
            action="shell",
            pid=pid,
            command=fallback_command,
            cwd=str(spec.directory),
            clear_recovery=True,
            detail="successful application exit returned to an interactive shell",
        )

    def recover_dead_payload(self, record: SessionRecord) -> DeadPayloadRecoveryResult:
        """Recover a dead pane according to exit status and durable policy.

        ``on-failure`` preserves the terminal/application distinction: a normal
        exit (0) or operator interrupt (130) becomes a shell in the same pane;
        other failures resume the durable payload. ``always`` continues to
        restart after every exit. Manual/never policies remain fail-closed.
        """

        if getattr(record, "external", False):
            raise TmuxError("adopted external sessions cannot be recovered")
        recipe = self._durable_recipe(record)
        if not automatic_recovery_allowed(recipe):
            raise TmuxError(
                f"restart policy {recipe.restart_policy!r} does not authorize recovery"
            )
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        raw_scope = (getattr(record, "auth_scope_id", None) or "").strip()
        if bool(raw_home) != bool(raw_scope):
            raise TmuxError("optional private auth metadata is incomplete")

        def lifecycle_or_result() -> _PaneLifecycle | DeadPayloadRecoveryResult:
            lifecycle = self._pane_lifecycle(record)
            if lifecycle is None:
                raise TmuxError("managed pane lifecycle could not be verified")
            if lifecycle.dead:
                return lifecycle
            pid = lifecycle.pane_pid
            if pid is None or pid <= 1:
                raise TmuxError("live managed pane has no authoritative pid")
            # Bind the no-op result to two same-query lifecycle snapshots. A
            # cached pane map is deliberately absent here: a target may retain
            # its pane id while a respawn changes its process identity.
            confirmed = self._pane_lifecycle(record)
            if (
                confirmed is None
                or confirmed.dead
                or confirmed.pane_id != lifecycle.pane_id
                or confirmed.pane_pid != pid
            ):
                raise TmuxError("managed pane changed while live state was verified")
            return DeadPayloadRecoveryResult(
                action="alive",
                pid=pid,
                detail="managed payload is already live; no replacement was performed",
            )

        if raw_home:
            with auth_transaction_lock(record):
                observed = lifecycle_or_result()
                if isinstance(observed, DeadPayloadRecoveryResult):
                    if (
                        self._exact_codex_recovery(record) is not None
                        or self._exact_claude_recovery(record) is not None
                    ):
                        previous_pid = observed.pid
                        self._recover_exact_over_idle_placeholder(record)
                        recovered_pid = self.authoritative_live_pane_pid(record)
                        if recovered_pid is None or recovered_pid <= 1:
                            raise TmuxError(
                                "exact recovery did not produce an authoritative live pane"
                            )
                        if recovered_pid != previous_pid:
                            return DeadPayloadRecoveryResult(
                                action="restarted",
                                pid=recovered_pid,
                                detail="exact payload resumed over its idle placeholder",
                            )
                    return observed
                lifecycle = observed
                if (
                    recipe.restart_policy == "on-failure"
                    and lifecycle.exit_status in {0, 130}
                ):
                    lease = _acquire_auth_lifetime_lease(record)
                    try:
                        if self._pane_lifecycle(record) != lifecycle:
                            raise TmuxError(
                                "dead pane changed while the shell bridge was prepared"
                            )
                        return self._respawn_clean_exit_shell(
                            record,
                            lifecycle,
                            lifetime_lease=lease,
                        )
                    finally:
                        lease.release()
        else:
            observed = lifecycle_or_result()
            if isinstance(observed, DeadPayloadRecoveryResult):
                return observed
            lifecycle = observed
            if (
                recipe.restart_policy == "on-failure"
                and lifecycle.exit_status in {0, 130}
            ):
                return self._respawn_clean_exit_shell(record, lifecycle)

        probe = replace(record, status="recovery-pending")
        self.create_or_resume(probe)
        pid = self._await_live_pane_pid(record, pane_id=lifecycle.pane_id)
        return DeadPayloadRecoveryResult(
            action="restarted",
            pid=pid,
            detail="failed application payload resumed from its durable recipe",
        )

    def create_or_resume(self, record: SessionRecord) -> None:
        self._create_or_resume(record, initial_grid=None)

    def create_or_resume_at_grid(
        self,
        record: SessionRecord,
        *,
        columns: int,
        rows: int,
    ) -> None:
        """Create a missing payload at the GUI's settled terminal geometry."""

        self._create_or_resume(
            record,
            initial_grid=self._validated_initial_grid(columns, rows),
        )

    def _create_or_resume(
        self,
        record: SessionRecord,
        *,
        initial_grid: tuple[int, int] | None,
    ) -> None:
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        raw_scope = (getattr(record, "auth_scope_id", None) or "").strip()
        if bool(raw_home) != bool(raw_scope):
            raise TmuxError("optional private auth metadata is incomplete")
        if raw_home:
            with auth_transaction_lock(record):
                if self._auth_session_exists(record):
                    lifecycle = self._pane_lifecycle(record)
                    if lifecycle is not None and lifecycle.dead:
                        lifetime_lease = _acquire_auth_lifetime_lease(record)
                        try:
                            self._respawn_dead_pane(
                                record,
                                lifecycle,
                                lifetime_lease=lifetime_lease,
                            )
                        finally:
                            if lifetime_lease.held:
                                lifetime_lease.release()
                    elif (
                        record.status.casefold() == "recovery-pending"
                        and (
                            self._exact_codex_recovery(record) is not None
                            or self._exact_claude_recovery(record) is not None
                        )
                    ):
                        # Preserve the legacy higher-fidelity placeholder repair
                        # when a private live session has an exact native resume
                        # target. An unavailable lifecycle snapshot is not proof
                        # that the pane is dead, so this existing fail-closed path
                        # remains authoritative unless tmux explicitly reports
                        # pane_dead=1 above.
                        self._recover_exact_over_idle_placeholder(record)
                    return
                lifetime_lease = _acquire_auth_lifetime_lease(record)
                try:
                    create_kwargs: dict[str, object] = {
                        "require_absent": True,
                        "lifetime_lease": lifetime_lease,
                    }
                    if initial_grid is not None:
                        create_kwargs["initial_grid"] = initial_grid
                    self._create_or_resume_unlocked(record, **create_kwargs)
                finally:
                    if lifetime_lease.held:
                        lifetime_lease.release()
            return

        if self.session_exists(record):
            lifecycle = self._pane_lifecycle(record)
            if lifecycle is not None and lifecycle.dead:
                self._respawn_dead_pane(record, lifecycle)
            return
        if initial_grid is None:
            self._create_or_resume_unlocked(record)
        else:
            self._create_or_resume_unlocked(record, initial_grid=initial_grid)

    def gateway_model_status(
        self,
        record: SessionRecord,
        *,
        cache_path: str | Path | None = None,
    ) -> GatewayModelStatus:
        """Compare one live gateway-model process with the validated cache."""

        try:
            pid = self.pane_pid(record)
        except (OSError, TmuxError):
            pid = None
        return classify_gateway_model_status(
            record,
            process_started_at=process_start_epoch(pid),
            cache_path=cache_path,
        )

    def restart_exact_payload(self, record: SessionRecord) -> int:
        """Atomically re-exec one exact recovered payload in its existing pane.

        This is the safe refresh primitive for process-local model inventories
        and other launcher state. The current pane must still own exactly the
        durable recovery command and directory recorded for this tab. Input is
        disabled and the process tree is frozen before the final revalidation;
        any mismatch fails before ``respawn-pane``. The tmux session, pane id,
        Termdeck tab, window, and conversation UUID remain unchanged.
        """

        if getattr(record, "external", False):
            raise TmuxError("adopted external sessions cannot be refreshed")
        exact_recovery = self._exact_codex_recovery(record)
        if exact_recovery is None:
            exact_recovery = self._exact_claude_recovery(record)
        if exact_recovery is None:
            raise TmuxError("exact recovery command is unavailable")

        raw_recovery_cwd = (getattr(record, "recovery_cwd", None) or "").strip()
        recovery_cwd = Path(raw_recovery_cwd).expanduser()
        if (
            not raw_recovery_cwd
            or not recovery_cwd.is_absolute()
            or not recovery_cwd.is_dir()
        ):
            raise TmuxError(
                "exact payload refresh requires an existing absolute recovery directory"
            )
        try:
            recovery_cwd = recovery_cwd.resolve()
        except OSError:
            pass

        lock = (
            auth_transaction_lock(record)
            if (getattr(record, "codex_home", None) or "").strip()
            else nullcontext()
        )
        with lock:
            return self._restart_exact_payload_unlocked(
                record,
                exact_recovery=exact_recovery,
                recovery_cwd=recovery_cwd,
            )

    def _restart_exact_payload_unlocked(
        self,
        record: SessionRecord,
        *,
        exact_recovery: str,
        recovery_cwd: Path,
    ) -> int:
        identity = self._inspect_auth_pane(record)
        if identity is None:
            raise TmuxError("exact payload pane could not be inspected")
        if self._auth_process_identity(identity.pane_pid) is None:
            raise TmuxError("exact payload process identity is unavailable")

        def require_exact_binding() -> None:
            try:
                observed, observed_command, observed_cwd = observe_recovery_metadata(
                    identity.pane_pid
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise TmuxError(
                    f"exact payload recovery state could not be inspected: {exc}"
                ) from exc
            try:
                cwd_matches = bool(
                    observed_cwd
                    and Path(observed_cwd).expanduser().resolve() == recovery_cwd
                )
            except OSError:
                cwd_matches = observed_cwd == str(recovery_cwd)
            if not observed or observed_command != exact_recovery or not cwd_matches:
                raise TmuxError(
                    "live pane owns a different exact recovery session; refusing to replace it"
                )

        require_exact_binding()
        input_was_off = self._disable_auth_pane_input(identity.pane_id)
        suspended: tuple[tuple[int, int, bool], ...] = ()
        lifetime_lease: _AuthLifetimeLease | None = None
        respawned = False
        try:
            suspended = self._suspend_auth_process_tree(
                identity.pane_pid,
                allow_running_root=True,
            )
            if self._inspect_auth_pane(record) != identity:
                raise TmuxError(
                    "live pane changed while exact payload refresh was being quiesced"
                )
            require_exact_binding()

            if (getattr(record, "codex_home", None) or "").strip():
                lifetime_lease = _acquire_auth_lifetime_lease(record)
                if self._inspect_auth_pane(record) != identity:
                    raise TmuxError(
                        "live pane changed while its recovery scope was leased"
                    )
                require_exact_binding()

            probe = replace(record, status="recovery-pending")
            spec = self._launch_spec(probe)
            if not spec.application_native_resume or exact_recovery not in spec.command:
                raise TmuxError(
                    "durable launch recipe did not resolve to the exact recovery command"
                )

            # The replacement launcher may need to acquire the same lifetime
            # locks. Release immediately before the atomic pane mutation while
            # the auth transaction lock still serializes this operation.
            if lifetime_lease is not None:
                lifetime_lease.release()
                lifetime_lease = None
            args = ["respawn-pane", "-k", "-t", identity.pane_id]
            args.extend(self._environment_args(spec.environment))
            args.extend(["-c", str(spec.directory), spec.command])
            self._run(args)
            respawned = True
            self._run(
                ["set-option", "-t", record.tmux_name, "remain-on-exit", "on"],
                check=False,
            )
            self._kill_quiesced_auth_processes(suspended)
            created, readiness_error = self._await_exact_pane_respawn_ready(
                record,
                previous=identity,
                expected_recovery_command=exact_recovery,
                expected_recovery_cwd=str(recovery_cwd),
            )
            if created is None or readiness_error:
                raise TmuxError(
                    readiness_error or "refreshed exact payload was not ready"
                )
            self._restore_auth_pane_input(identity.pane_id, input_was_off)
            self.invalidate_cache()
            return created.pane_pid
        except Exception as exc:
            rollback_error = ""
            if not respawned:
                try:
                    self._resume_auth_processes(suspended)
                except (OSError, TmuxError) as resume_exc:
                    rollback_error = f"; old process could not be resumed: {resume_exc}"
            try:
                self._restore_auth_pane_input(identity.pane_id, input_was_off)
            except (OSError, TmuxError) as input_exc:
                rollback_error += f"; terminal input could not be restored: {input_exc}"
            if isinstance(exc, TmuxError):
                raise TmuxError(f"{exc}{rollback_error}") from exc
            raise TmuxError(
                f"exact payload could not be refreshed: {exc}{rollback_error}"
            ) from exc
        finally:
            if lifetime_lease is not None:
                lifetime_lease.release()

    def _recover_exact_over_idle_placeholder(self, record: SessionRecord) -> None:
        """Replace one proven idle placeholder without removing its tmux pane.

        A failed exact launch can leave ``remain-on-exit`` or a trailing shell
        alive.  Merely checking that the tmux target exists then turns durable
        ``recovery-pending`` state into a false success: the tab is visible but
        its Codex/Claude conversation is gone.  Respawning the same pane keeps
        every GTK/tmux client attached while replacing only a verified idle
        shell.  Any foreground/background job, different recovery UUID, or
        unverifiable process identity fails closed.
        """

        exact_recovery = self._exact_codex_recovery(record)
        if exact_recovery is None:
            exact_recovery = self._exact_claude_recovery(record)
        if exact_recovery is None:
            raise TmuxError("exact recovery command is unavailable")

        raw_recovery_cwd = (getattr(record, "recovery_cwd", None) or "").strip()
        recovery_cwd = Path(raw_recovery_cwd).expanduser()
        if (
            not raw_recovery_cwd
            or not recovery_cwd.is_absolute()
            or not recovery_cwd.is_dir()
        ):
            raise TmuxError("exact recovery requires an existing absolute recovery directory")
        try:
            recovery_cwd = recovery_cwd.resolve()
        except OSError:
            pass

        identity = self._inspect_auth_pane(record)
        if identity is None:
            raise TmuxError("exact recovery placeholder pane could not be inspected")
        process_identity = self._auth_process_identity(identity.pane_pid)
        if process_identity is None:
            raise TmuxError("exact recovery placeholder process identity is unavailable")

        try:
            observed, observed_command, observed_cwd = observe_recovery_metadata(
                identity.pane_pid
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise TmuxError(f"exact recovery placeholder could not be inspected: {exc}") from exc
        if observed_command is not None:
            try:
                cwd_matches = (
                    observed_cwd is not None
                    and Path(observed_cwd).expanduser().resolve() == recovery_cwd
                )
            except OSError:
                cwd_matches = observed_cwd == str(recovery_cwd)
            if observed_command == exact_recovery and cwd_matches:
                return
            raise TmuxError(
                "live pane owns a different exact recovery session; refusing to replace it"
            )
        if not observed or not self._process_tree_is_idle_shell(identity.pane_pid):
            raise TmuxError(
                "exact recovery requires a readable idle placeholder shell with no jobs"
            )

        input_was_off = self._disable_auth_pane_input(identity.pane_id)
        suspended: tuple[tuple[int, int, bool], ...] = ()
        lifetime_lease: _AuthLifetimeLease | None = None
        respawned = False
        try:
            suspended = self._suspend_auth_process_tree(
                identity.pane_pid,
                allow_running_root=True,
            )
            if self._inspect_auth_pane(record) != identity:
                raise TmuxError("placeholder pane changed while exact recovery was being quiesced")
            if not self._process_tree_is_idle_shell(identity.pane_pid):
                raise TmuxError("placeholder shell started a job during exact recovery")

            lifetime_lease = _acquire_auth_lifetime_lease(record)
            if self._inspect_auth_pane(record) != identity:
                raise TmuxError("placeholder pane changed while its recovery scope was leased")

            raw_home = (getattr(record, "codex_home", None) or "").strip()
            try:
                migrate_quiescent_codex_configuration(raw_home)
                ensure_private_codex_state(raw_home, require_complete=True)
            except (CodexAuthError, CodexConfigScopeError) as exc:
                raise TmuxError(str(exc)) from exc
            if exact_recovery == self._exact_codex_recovery(record):
                self._hydrate_private_codex_recovery(record, exact_recovery)

            codex_home = Path(raw_home).expanduser().resolve(strict=False)
            isolated_path = self._private_auth_path(codex_home)
            command = exact_recovery
            if self._exact_codex_recovery(record) is not None:
                command = self._private_exact_codex_launch_command(
                    record,
                    exact_recovery,
                    isolated_path,
                )
            try:
                launch_argv = shlex.split(record.command.strip())
            except ValueError:
                launch_argv = []
            if (
                self._exact_codex_recovery(record) is not None
                and len(launch_argv) == 1
                and Path(launch_argv[0]).name.casefold() in self._SUPPORTED_IDLE_SHELLS
            ):
                trailing_shell = shlex.join(launch_argv)
                if Path(launch_argv[0]).name.casefold() == "bash":
                    try:
                        trailing_shell = prepare_private_bash_launch_command(
                            codex_home,
                            auth_scope_id=(
                                getattr(record, "auth_scope_id", "") or ""
                            ),
                            shell_path=launch_argv[0],
                        )
                    except CodexConfigScopeError as exc:
                        raise TmuxError(
                            f"private Bash environment could not be prepared: {exc}"
                        ) from exc
                command = f"{command}; exec {trailing_shell}"

            # The new contained launcher must acquire the same lifetime locks.
            # Release immediately before respawn: the old shell is stopped and
            # the auth transaction lock still prevents another TermDeck
            # mutation, while retaining this lease would make the child reject
            # its own startup as a concurrent scope owner.
            lifetime_lease.release()
            lifetime_lease = None
            self._run(
                [
                    "respawn-pane",
                    "-k",
                    "-t",
                    identity.pane_id,
                    "-e",
                    f"CODEX_HOME={codex_home}",
                    "-e",
                    f"TERMDECK_AUTH_SCOPE_ID={getattr(record, 'auth_scope_id', '') or ''}",
                    "-e",
                    f"CODEX_TERMDECK_GATE_ROOT={auth_lifetime_gate_root(record)}",
                    "-e",
                    f"PATH={isolated_path}",
                    "-c",
                    str(recovery_cwd),
                    command,
                ]
            )
            respawned = True
            self._kill_quiesced_auth_processes(suspended)
            created, readiness_error = self._await_exact_pane_respawn_ready(
                record,
                previous=identity,
                expected_recovery_command=exact_recovery,
                expected_recovery_cwd=str(recovery_cwd),
            )
            if created is None or readiness_error:
                raise TmuxError(
                    readiness_error or "exact recovery pane was not ready after respawn"
                )
            self._restore_auth_pane_input(identity.pane_id, input_was_off)
            self.invalidate_cache()
        except Exception as exc:
            if not respawned:
                try:
                    self._resume_auth_processes(suspended)
                except (OSError, TmuxError) as resume_exc:
                    try:
                        self._restore_auth_pane_input(identity.pane_id, input_was_off)
                    except (OSError, TmuxError) as input_exc:
                        raise TmuxError(
                            f"{exc}; placeholder shell could not be resumed: {resume_exc}; "
                            f"terminal input could not be restored: {input_exc}"
                        ) from exc
                    raise TmuxError(
                        f"{exc}; placeholder shell could not be resumed: {resume_exc}"
                    ) from exc
            try:
                self._restore_auth_pane_input(identity.pane_id, input_was_off)
            except (OSError, TmuxError) as input_exc:
                raise TmuxError(
                    f"{exc}; terminal input could not be restored: {input_exc}"
                ) from exc
            if isinstance(exc, TmuxError):
                raise
            raise TmuxError(f"exact recovery pane could not be respawned: {exc}") from exc
        finally:
            if lifetime_lease is not None:
                lifetime_lease.release()

    @staticmethod
    def _private_exact_codex_launch_command(
        record: SessionRecord,
        recovery_command: str,
        isolated_path: str,
    ) -> str:
        """Bind exact recovery to this scope without trusting ambient PATH.

        Host launchers, tmux servers, and interactive shell startup files can
        all retain a different tab's private ``bin`` directory.  Resolving the
        bare ``codex`` token through that PATH selects the wrong wrapper and
        either leaks credentials or (with the strict launcher) aborts with a
        scope mismatch.  Execute this scope's generated wrapper by absolute
        path and pass the complete private tuple explicitly.
        """

        try:
            recovery_argv = shlex.split(recovery_command)
        except ValueError as exc:
            raise TmuxError("private Codex recovery command is malformed") from exc
        exact = safe_codex_recovery_command(recovery_argv)
        if exact is None:
            raise TmuxError("private Codex launch requires an exact recovery command")
        argv = shlex.split(exact)
        if "--dangerously-bypass-hook-trust" not in argv[1:]:
            # This is an unattended crash/restart path, not a new interactive
            # request to trust hooks. The hook files and their canonical links
            # were already validated by the scope migration; stopping at a
            # prompt would recreate the same false-running placeholder bug.
            argv.insert(1, "--dangerously-bypass-hook-trust")
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        home = Path(raw_home).expanduser().resolve(strict=False)
        wrapper = home / "bin" / "codex"
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise TmuxError("private Codex wrapper is missing or not executable")
        return shlex.join(
            [
                "/usr/bin/env",
                f"CODEX_HOME={home}",
                f"TERMDECK_AUTH_SCOPE_ID={getattr(record, 'auth_scope_id', '') or ''}",
                f"CODEX_TERMDECK_GATE_ROOT={auth_lifetime_gate_root(record)}",
                f"PATH={isolated_path}",
                str(wrapper),
                *argv[1:],
            ]
        )

    def _await_exact_pane_respawn_ready(
        self,
        record: SessionRecord,
        *,
        previous: _AuthPaneIdentity,
        expected_recovery_command: str,
        expected_recovery_cwd: str,
        timeout: float = 15.0,
    ) -> tuple[_AuthPaneIdentity | None, str]:
        """Prove an in-place pane respawn took over the expected conversation."""

        deadline = time.monotonic() + max(0.05, timeout)
        last_identity: _AuthPaneIdentity | None = None
        last_detail = "respawned pane was not ready"
        while True:
            try:
                identity = self._inspect_auth_pane(record)
            except (OSError, TmuxError) as exc:
                identity = None
                last_detail = f"respawned pane could not be inspected: {exc}"
            last_identity = identity
            if identity is not None:
                process_identity = self._auth_process_identity(identity.pane_pid)
                if (
                    identity.session_created != previous.session_created
                    or identity.pane_id != previous.pane_id
                ):
                    last_detail = "respawn replaced the bound tmux session or pane identity"
                elif identity.pane_pid == previous.pane_pid:
                    last_detail = "respawned pane retained the old shell process"
                elif process_identity is None:
                    last_detail = "respawned pane process identity was unavailable"
                elif (
                    bool((getattr(record, "codex_home", None) or "").strip())
                    and not self._private_auth_process_tree_environment_ready(
                        record,
                        identity.pane_pid,
                    )
                ):
                    last_detail = "respawned pane did not inherit the private auth environment"
                else:
                    try:
                        observed, command, cwd = observe_recovery_metadata(identity.pane_pid)
                    except (OSError, RuntimeError, ValueError) as exc:
                        last_detail = f"exact recovery could not be observed: {exc}"
                    else:
                        if (
                            observed
                            and command == expected_recovery_command
                            and cwd == expected_recovery_cwd
                        ):
                            return identity, ""
                        last_detail = "respawned pane has not taken over the exact recovery UUID"
            if time.monotonic() >= deadline:
                return last_identity, last_detail
            time.sleep(0.025)

    def _create_or_resume_unlocked(
        self,
        record: SessionRecord,
        *,
        require_absent: bool = False,
        lifetime_lease: _AuthLifetimeLease | None = None,
        initial_grid: tuple[int, int] | None = None,
    ) -> None:
        raw_codex_home = (getattr(record, "codex_home", None) or "").strip()
        if raw_codex_home:
            if not _auth_transaction_is_held(record):
                raise TmuxError(
                    "private auth launch requires the scope auth transaction lock"
                )
            if (
                lifetime_lease is None
                or not lifetime_lease.matches(
                    _auth_legacy_lifetime_lock_path(record),
                    _auth_lifetime_lock_path(record),
                )
            ):
                raise TmuxError("private auth launch requires the scope lifetime lease")
            if self._auth_session_exists(record):
                if not require_absent:
                    return
                raise TmuxError(
                    "another tmux session claimed this terminal name before recovery"
                )
        elif require_absent:
            if self._auth_session_exists(record):
                raise TmuxError(
                    "another tmux session claimed this terminal name before recovery"
                )
        elif self.session_exists(record):
            return
        if getattr(record, "external", False):
            raise TmuxError(f"adopted tmux session not found: {record.tmux_name}")

        recipe = self._durable_recipe(record)
        status = record.status.casefold()
        if status in {"stopped", "archived"}:
            raise TmuxError("intentionally stopped or archived terminal will not auto-restart")
        if status != "new" and not automatic_recovery_allowed(recipe):
            raise TmuxError(
                f"restart policy {recipe.restart_policy!r} does not authorize missing-session recovery"
            )
        spec = self._launch_spec(record)
        launch_directory = spec.directory
        command = spec.command
        # tmux accepts the command as one shell command argument, which preserves
        # normal shell syntax for commands such as "npm run dev" or "codex".
        launch_args = [
            "new-session",
            "-d",
            "-s",
            record.tmux_name,
        ]
        if initial_grid is not None:
            columns, rows = initial_grid
            launch_args.extend(["-x", str(columns), "-y", str(rows)])
        launch_args.extend(self._environment_args(spec.environment))
        launch_args.extend([
            "-c",
            str(launch_directory),
            command,
        ])
        if raw_codex_home and lifetime_lease is not None:
            lifetime_lease.release()
        self._run(launch_args)
        # Make the session visually clean when attached.
        self._run(["set-option", "-t", record.tmux_name, "status", "off"], check=False)
        self._run(["set-option", "-t", record.tmux_name, "remain-on-exit", "on"], check=False)
        self.invalidate_cache()

    def attach_command(self, record: SessionRecord) -> list[str]:
        return [self.executable, "attach-session", "-t", record.tmux_name]

    def configure_interactive_session(self, record: SessionRecord) -> None:
        """Keep pointer input application-owned with bounded pane history.

        TermDeck and VTE own scrolling and focus. Enabling tmux mouse capture
        makes an ordinary wheel gesture enter copy mode, where subsequent keys
        move a history cursor instead of reaching a full-screen TUI composer.
        Explicitly disable that capture while retaining a finite history limit.
        Failure is non-fatal: attaching still works.
        """

        if not self.available:
            return
        with self._history_session_lock_for(record.id):
            self._history_pop(record.id)
        now = time.monotonic()
        if now < self._interactive_config_retry_at:
            return
        result = self._run(
            [
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-t",
                record.tmux_name,
                "history-limit",
                str(self.history_limit),
                ";",
                "set-option",
                "-w",
                "-t",
                self.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                self.pane_target(record),
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            self._interactive_config_retry_at = now + self.failure_backoff

    def _history_session_lock_for(self, session_id: str) -> threading.RLock:
        """Return one stable serial lock without holding the map lock over I/O."""

        with self._history_map_lock:
            lock = self._history_session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._history_session_locks[session_id] = lock
            return lock

    def _history_get(self, session_id: str) -> BackendHistoryState | None:
        with self._history_map_lock:
            return self._scroll_views.get(session_id)

    def _history_set(self, session_id: str, state: BackendHistoryState) -> None:
        with self._history_map_lock:
            self._scroll_views[session_id] = state

    def _history_pop(self, session_id: str) -> BackendHistoryState | None:
        with self._history_map_lock:
            return self._scroll_views.pop(session_id, None)

    def _history_contains(self, session_id: str) -> bool:
        with self._history_map_lock:
            return session_id in self._scroll_views

    def scroll_view_active(self, record: SessionRecord) -> bool:
        return self._history_contains(record.id)

    def scroll_state(self, record: SessionRecord) -> BackendHistoryState | None:
        """Return immutable tracked history state for UI synchronization/tests."""

        return self._history_get(record.id)

    def _query_scroll_context(
        self,
        target: str,
    ) -> tuple[bool, bool, str, bool, bool] | None:
        current = self._run(
            [
                "display-message",
                "-p",
                "-t",
                target,
                "#{alternate_on}\t#{pane_in_mode}\t#{pane_current_command}\t#{mouse_any_flag}\t#{mouse_sgr_flag}",
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if current.returncode != 0:
            return None
        fields = current.stdout.rstrip("\n").split("\t", 4)
        if len(fields) != 5:
            return None
        return (
            fields[0] == "1",
            fields[1] == "1",
            Path(fields[2].strip()).name.casefold(),
            fields[3] == "1",
            fields[4] == "1",
        )

    @staticmethod
    def _parse_tmux_history_position(output: str) -> tuple[int, int] | None:
        fields = output.strip().split("\t")
        if len(fields) != 3:
            return None
        try:
            pane_in_mode = int(fields[0])
            position = max(0, int(fields[1]))
            extent = max(0, int(fields[2]))
        except (TypeError, ValueError):
            return None
        if pane_in_mode not in {0, 1}:
            return None
        return position, extent

    @staticmethod
    def _codex_navigation_args(target: str, amount: int) -> list[str]:
        repetitions = max(1, abs(int(amount)))
        key = "Up" if amount < 0 else "Down"
        if repetitions > 24:
            # A scrollbar drag can jump hundreds of logical transcript lines.
            # Page navigation bounds the tmux argv and child input volume while
            # preserving precise wheel-sized movement.
            repetitions = (repetitions + 23) // 24
            key = "PageUp" if amount < 0 else "PageDown"
        return [
            "send-keys",
            "-t",
            target,
            "-N",
            str(repetitions),
            key,
        ]

    def _classify_scroll_owner(
        self,
        record: SessionRecord,
        target: str,
        amount: int,
        *,
        context: tuple[bool, bool, str, bool, bool] | None = None,
    ) -> BackendHistoryState | ScrollOutcome:
        """Classify a live pane without ever treating VTE as history authority."""

        if context is None:
            context = self._query_scroll_context(target)
            if context is None:
                return unhandled_scroll(amount, detail="tmux owner query failed")
        alternate_on, pane_in_mode, command, mouse_reporting, mouse_sgr = context
        now = time.monotonic()
        if command == "codex" or command.startswith("codex-"):
            return BackendHistoryState(
                owner=HistoryOwner.CODEX,
                depth=0,
                managed_open=False,
                command=command,
                validated_at=now,
            )
        if alternate_on:
            # Full-screen applications own their virtual scrollback. tmux copy
            # mode can expose only incidental pre-alternate-screen output and
            # must never replace the child's own mouse/wheel protocol.
            if pane_in_mode:
                restored = self._run(
                    ["copy-mode", "-q", "-t", target],
                    check=False,
                    timeout=self.query_timeout,
                )
                if restored.returncode != 0:
                    return unhandled_scroll(
                        amount,
                        detail="could not leave stale tmux copy mode",
                    )
            self._history_pop(record.id)
            if mouse_reporting:
                return passthrough_scroll(
                    amount,
                    detail=f"alternate-screen mouse owner {command or 'unknown'}",
                    protocol=(
                        ChildInputProtocol.SGR_MOUSE
                        if mouse_sgr
                        else ChildInputProtocol.LEGACY_MOUSE
                    ),
                )
            return ScrollOutcome(
                disposition=ScrollDisposition.UNHANDLED,
                owner=HistoryOwner.CHILD_TUI,
                edge=EdgeKnowledge.UNKNOWN,
                requested_lines=amount,
                detail=(
                    f"alternate-screen owner {command or 'unknown'} has no mouse "
                    "reporting; wheel input was consumed"
                ),
            )
        if amount > 0:
            # Primary-screen down from the live edge is a no-op. Querying the
            # pane first is essential: a mouse-aware alternate-screen TUI owns
            # both wheel directions and must receive the downward gesture.
            return unhandled_scroll(
                amount,
                detail="already at live edge",
                edge=EdgeKnowledge.LIVE_EXACT,
                depth=0,
            )
        return BackendHistoryState(
            owner=HistoryOwner.TMUX,
            depth=0,
            extent=self.history_limit,
            command=command,
            validated_at=now,
        )

    def _validated_scroll_state(
        self,
        record: SessionRecord,
        target: str,
        amount: int,
    ) -> BackendHistoryState | ScrollOutcome:
        state = self._history_get(record.id)
        if state is None:
            return self._classify_scroll_owner(record, target, amount)
        now = time.monotonic()
        # Codex transcript navigation is child input. Revalidate its foreground
        # owner before every navigation gesture so a fast Codex exit cannot
        # turn a stale transcript Down/C-t into shell input. Tmux copy-mode
        # scrolling stays inside tmux and may use the short validation TTL.
        if (
            state.owner is not HistoryOwner.CODEX
            and now - state.validated_at <= self._history_owner_validation_ttl
        ):
            return state
        context = self._query_scroll_context(target)
        if context is None:
            if state.owner is HistoryOwner.CODEX:
                return ScrollOutcome(
                    disposition=ScrollDisposition.UNHANDLED,
                    owner=HistoryOwner.CODEX,
                    edge=EdgeKnowledge.UNKNOWN,
                    requested_lines=amount,
                    depth=state.depth,
                    extent=state.extent,
                    managed_open=state.managed_open,
                    detail="could not revalidate the Codex transcript owner",
                )
            # A failed tmux owner query is not evidence of the live edge. Copy
            # mode commands remain tmux-local, so preserve that tracked state.
            return replace(state, validated_at=now)
        alternate_on, pane_in_mode, command, _mouse_reporting, _mouse_sgr = context
        if state.owner is HistoryOwner.CODEX:
            if command == "codex" or command.startswith("codex-"):
                return replace(state, command=command, validated_at=now)
            self._history_pop(record.id)
            return self._classify_scroll_owner(
                record, target, amount, context=context
            )
        if state.owner is HistoryOwner.TMUX:
            if not alternate_on and pane_in_mode:
                return replace(state, command=command, validated_at=now)
            self._history_pop(record.id)
            return self._classify_scroll_owner(
                record, target, amount, context=context
            )
        self._history_pop(record.id)
        return self._classify_scroll_owner(
            record, target, amount, context=context
        )

    def scroll_session(self, record: SessionRecord, lines: int) -> ScrollOutcome:
        """Route one scroll through its canonical owner and return edge proof.

        ``lines < 0`` moves away from live output; ``lines > 0`` moves toward
        it. VTE's emulator adjustment is intentionally absent from this API.
        """

        amount = int(lines)
        if not amount or not self.available:
            return unhandled_scroll(
                amount,
                detail="zero movement or tmux unavailable",
            )
        target = self.pane_target(record)
        with self._history_session_lock_for(record.id):
            classified = self._validated_scroll_state(record, target, amount)
            if isinstance(classified, ScrollOutcome):
                return classified
            state = classified

            if state.owner is HistoryOwner.CODEX:
                if not state.managed_open:
                    opened = self._run(
                        ["send-keys", "-t", target, "C-t"],
                        check=False,
                        timeout=self.query_timeout,
                    )
                    if opened.returncode != 0:
                        return ScrollOutcome(
                            disposition=ScrollDisposition.UNHANDLED,
                            owner=HistoryOwner.CODEX,
                            edge=EdgeKnowledge.UNKNOWN,
                            requested_lines=amount,
                            depth=state.depth,
                            managed_open=False,
                            detail="could not open Codex transcript",
                        )
                    state = replace(
                        state,
                        managed_open=True,
                        validated_at=time.monotonic(),
                    )
                    self._history_set(record.id, state)

                effective = amount
                if amount > 0:
                    effective = min(amount, state.depth)
                target_depth = max(0, state.depth - effective)
                if effective:
                    moved = self._run(
                        self._codex_navigation_args(target, effective),
                        check=False,
                        timeout=self.query_timeout,
                    )
                    if moved.returncode != 0:
                        # Opening may already have succeeded. Preserve that fact
                        # so live input cannot blindly enter the transcript.
                        self._history_set(record.id, state)
                        return ScrollOutcome(
                            disposition=ScrollDisposition.UNHANDLED,
                            owner=HistoryOwner.CODEX,
                            edge=EdgeKnowledge.UNKNOWN,
                            requested_lines=amount,
                            depth=state.depth,
                            managed_open=True,
                            detail="Codex transcript navigation failed",
                        )
                state = replace(
                    state,
                    depth=target_depth,
                    validated_at=time.monotonic(),
                )
                self._history_set(record.id, state)
                return ScrollOutcome(
                    disposition=ScrollDisposition.HANDLED,
                    owner=HistoryOwner.CODEX,
                    edge=(
                        EdgeKnowledge.LIVE_EXACT
                        if target_depth == 0
                        else EdgeKnowledge.HISTORY_EXACT
                    ),
                    requested_lines=amount,
                    depth=target_depth,
                    managed_open=True,
                )

            if state.owner is HistoryOwner.TMUX:
                estimated_depth = max(0, state.depth - amount)
                commands: list[str] = []
                if not self._history_contains(record.id):
                    commands.extend(["copy-mode", "-t", target, ";"])
                commands.extend(
                    [
                        "send-keys",
                        "-X",
                        "-t",
                        target,
                        "-N",
                        str(max(1, abs(amount))),
                        "scroll-up" if amount < 0 else "scroll-down",
                        ";",
                        "display-message",
                        "-p",
                        "-t",
                        target,
                        "#{pane_in_mode}\t#{scroll_position}\t#{history_size}",
                    ]
                )
                moved = self._run(
                    commands,
                    check=False,
                    timeout=self.query_timeout,
                )
                if moved.returncode != 0:
                    return ScrollOutcome(
                        disposition=ScrollDisposition.UNHANDLED,
                        owner=HistoryOwner.TMUX,
                        edge=EdgeKnowledge.UNKNOWN,
                        requested_lines=amount,
                        depth=state.depth,
                        extent=state.extent,
                        detail="tmux history navigation failed",
                    )
                exact = self._parse_tmux_history_position(moved.stdout)
                if exact is None:
                    state = replace(
                        state,
                        depth=estimated_depth,
                        validated_at=time.monotonic(),
                    )
                    self._history_set(record.id, state)
                    return ScrollOutcome(
                        disposition=ScrollDisposition.HANDLED,
                        owner=HistoryOwner.TMUX,
                        edge=EdgeKnowledge.UNKNOWN,
                        requested_lines=amount,
                        depth=estimated_depth,
                        extent=state.extent,
                        detail="tmux did not return an exact history position",
                    )
                position, extent = exact
                state = replace(
                    state,
                    depth=position,
                    extent=extent,
                    validated_at=time.monotonic(),
                )
                self._history_set(record.id, state)
                return ScrollOutcome(
                    disposition=ScrollDisposition.HANDLED,
                    owner=HistoryOwner.TMUX,
                    edge=(
                        EdgeKnowledge.LIVE_EXACT
                        if position == 0
                        else EdgeKnowledge.HISTORY_EXACT
                    ),
                    requested_lines=amount,
                    depth=position,
                    extent=extent,
                )

            return passthrough_scroll(amount, detail="history is child-owned")

    def restore_live_session(self, record: SessionRecord, *, force: bool = False) -> bool:
        """Return a focused pane from historical copy mode to its live TUI."""

        if not self.available:
            return False
        now = time.monotonic()
        if not force and now < self._interactive_config_retry_at:
            return False
        with self._history_session_lock_for(record.id):
            state = self._history_get(record.id)
            target = self.pane_target(record)
            commands = []
            if (
                state is not None
                and state.owner is HistoryOwner.CODEX
                and state.managed_open
            ):
                context = self._query_scroll_context(target)
                if context is None:
                    # Unknown ownership is not permission to send the Codex
                    # transcript toggle or to release the user's first key.
                    return False
                _alternate_on, _pane_in_mode, command, _mouse_reporting, _mouse_sgr = context
                if command == "codex" or command.startswith("codex-"):
                    commands = [
                        "send-keys",
                        "-t",
                        target,
                        "C-t",
                        ";",
                    ]
                else:
                    # Codex already exited. The tracked transcript cannot exist
                    # in this foreground process, so clear only the stale
                    # owner record and perform tmux-local live cleanup below.
                    state = None
                    self._history_pop(record.id)
            commands.extend(
                [
                    "set-option",
                    "-t",
                    record.tmux_name,
                    "mouse",
                    "off",
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    target,
                    "window-size",
                    "latest",
                    ";",
                    "copy-mode",
                    "-q",
                    "-t",
                    target,
                ]
            )
            result = self._run(
                commands,
                check=False,
                timeout=self.query_timeout,
            )
            if result.returncode != 0:
                self._interactive_config_retry_at = now + self.failure_backoff
                return False
            self._history_pop(record.id)
            return True

    def pane_target(self, record: SessionRecord) -> str:
        """Return a stable target for managed rows and an adopted pane for external rows.

        ``%pane_id`` values are scoped to one tmux server generation.  Persisted
        managed rows survive server restarts, so replaying their old explicit ID
        can address an unrelated pane or make a recreated live session look
        missing.  Managed sessions are single-pane and are therefore addressed by
        their durable session name; only adopted/external rows may retain a
        server-local explicit pane target.
        """

        explicit = getattr(record, "pane_target", None)
        if bool(getattr(record, "external", False)) and explicit:
            return explicit
        return f"{record.tmux_name}:0.0"

    def current_directory(self, record: SessionRecord) -> str | None:
        """Return the active pane's live working directory when tmux can report it."""

        if not self.available:
            return None
        result = self._run(
            [
                "display-message",
                "-p",
                "-t",
                self.pane_target(record),
                "#{pane_current_path}",
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        candidate = result.stdout.strip()
        return self._normalise_pane_directory(candidate)

    @staticmethod
    def _normalise_pane_directory(candidate: str) -> str | None:
        if not candidate:
            return None
        directory = Path(candidate).expanduser()
        if not directory.is_dir():
            return None
        try:
            directory = directory.resolve()
        except OSError:
            directory = directory.absolute()
        return str(directory)

    def current_directories(self, records: Sequence[SessionRecord]) -> dict[str, str | None]:
        """Resolve live pane directories for many sessions from one cached snapshot."""

        out: dict[str, str | None] = {record.id: None for record in records}
        if not records or not self.available:
            return out
        if self._load_pane_maps(len(records)) is None:
            return out
        for record in records:
            target = self.pane_target(record)
            candidate = self._pane_cwd_by_target.get(target)
            if candidate is None and target == f"{record.tmux_name}:0.0":
                candidate = self._pane_cwd_by_session_active.get(record.tmux_name)
            out[record.id] = self._normalise_pane_directory(candidate or "")
        return out

    def pane_pid(self, record: SessionRecord) -> int | None:
        """Return a pane pid while sharing one cached all-pane snapshot."""

        if not self.available:
            return None
        maps = self._load_pane_maps()
        if maps is not None:
            by_target, by_session_active = maps
            target = self.pane_target(record)
            if target in by_target:
                return by_target[target]
            if target == f"{record.tmux_name}:0.0":
                return by_session_active.get(record.tmux_name)
            return None
        if self._pane_snapshot_timed_out:
            return record.last_pid

        result = self._run(
            ["list-panes", "-t", self.pane_target(record), "-F", "#{pane_pid}\t#{pane_dead}"],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        first = result.stdout.strip().splitlines()[0:1]
        if not first:
            return None
        pid_text, _separator, dead_text = first[0].partition("\t")
        if dead_text.strip() not in {"", "0"}:
            return None
        try:
            return int(pid_text)
        except ValueError:
            return None

    @staticmethod
    def propagate_terminal_resize(
        pane_pid: int | None,
        *,
        codex_home: str | None = None,
        auth_scope_id: str | None = None,
    ) -> ResizeSignalResult:
        """Forward one resize only to the private Codex bound to this row.

        The process tree cannot appoint its own authentication identity. The
        expected private tuple is derived from the durable TermDeck record and
        must exactly match the unique witness discovered below the pane root.
        Generic shells, legacy rows without a complete scope, and malformed
        identities produce an honest no-op.
        """

        if pane_pid is None:
            return ResizeSignalResult(
                signalled=False,
                reason="pane root PID is unavailable",
                root_pid=0,
            )
        try:
            expected_private_tuple = private_launch_tuple_for_scope(
                str(codex_home or "").strip(),
                str(auth_scope_id or "").strip(),
            )
        except ValueError:
            return ResizeSignalResult(
                signalled=False,
                reason="persistent private Codex identity is unavailable",
                root_pid=int(pane_pid),
            )
        return _propagate_terminal_resize(
            int(pane_pid),
            expected_private_tuple=expected_private_tuple,
        )

    def is_codex_running(self, record: SessionRecord) -> bool:
        """Return true only when the live pane process tree proves Codex is running."""

        try:
            pane_pid = self.pane_pid(record)
            if pane_pid is None:
                return False
            return process_tree_contains_codex(pane_pid) is True
        except (OSError, RuntimeError, ValueError):
            # Process inspection is necessarily racy. A vanished or unreadable
            # tree is not sufficient evidence to appoint this tab as a captain.
            return False

    def recovery_readiness(self, record: SessionRecord) -> dict[str, object]:
        """Classify a managed Codex pane while its launcher is starting.

        A tmux pane can survive a failed launcher as an ordinary shell.  Treat
        that readable, childless shell as terminal failure so recovery does not
        hold the bounded queue until its long deadline.  A launcher/helper
        process remains pending, while a verified Codex descendant is ready.
        Unreadable or otherwise ambiguous process state stays pending.
        """

        pane_pid = self.authoritative_live_pane_pid(record)
        if pane_pid is None:
            return {"state": "unknown"}
        codex_state = process_tree_contains_codex(pane_pid)
        if codex_state is True:
            return {"state": "ready", "pid": pane_pid}
        if codex_state is None:
            return {"state": "pending", "pid": pane_pid}
        try:
            idle_shell = self._process_tree_is_idle_shell(pane_pid)
        except (OSError, RuntimeError, ValueError):
            return {"state": "pending", "pid": pane_pid}
        if idle_shell:
            return {
                "state": "failed",
                "pid": pane_pid,
                "detail": "managed Codex launcher exited to an idle shell",
            }
        return {"state": "pending", "pid": pane_pid}

    def _load_pane_maps(
        self,
        record_count: int = 1,
    ) -> tuple[dict[str, int], dict[str, int]] | None:
        """Return cached tmux pane maps, refreshing with one list-panes call.

        UI surfaces can refresh together; without this cache each window/pane
        asks tmux for the same pane table and the app feels sluggish.  The cache
        is deliberately short-lived so process controls remain fresh enough for
        interactive use while avoiding repeated subprocess spawns per repaint.
        """

        now = time.monotonic()
        if self.pane_cache_ttl and now < self._pane_cache_expires_at:
            return self._pane_cache_by_target, self._pane_cache_by_session_active
        if now < self._pane_snapshot_retry_at:
            return None

        self._pane_snapshot_timed_out = False
        result = self._run(
            [
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_dead}",
            ],
            check=False,
            timeout=self._query_timeout_for(record_count),
        )
        if result.returncode != 0:
            self._pane_cache_expires_at = 0.0
            self._pane_cache_by_target = {}
            self._pane_cache_by_session_active = {}
            self._pane_cwd_by_target = {}
            self._pane_cwd_by_session_active = {}
            self._pane_snapshot_timed_out = result.returncode == 124
            self._pane_snapshot_stale = True
            self._pane_snapshot_retry_at = now + self.failure_backoff
            return None

        by_target: dict[str, int] = {}
        by_session_active: dict[str, int] = {}
        cwd_by_target: dict[str, str] = {}
        cwd_by_session_active: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t", 6)
            if len(parts) not in {5, 6, 7}:
                continue
            session_name, window_index, pane_index, pane_id, pane_pid = parts[:5]
            pane_cwd = parts[5] if len(parts) >= 6 else ""
            pane_dead = parts[6] if len(parts) == 7 else "0"
            if pane_dead.strip() not in {"", "0"}:
                continue
            try:
                pid = int(pane_pid)
            except ValueError:
                continue
            target = f"{session_name}:{window_index}.{pane_index}"
            by_target[target] = pid
            if pane_cwd:
                cwd_by_target[target] = pane_cwd
            if pane_id:
                pane_key = pane_id if pane_id.startswith("%") else f"%{pane_id}"
                by_target[pane_key] = pid
                if pane_cwd:
                    cwd_by_target[pane_key] = pane_cwd
            by_session_active.setdefault(session_name, pid)
            if pane_cwd:
                cwd_by_session_active.setdefault(session_name, pane_cwd)

        self._pane_cache_by_target = by_target
        self._pane_cache_by_session_active = by_session_active
        self._pane_cwd_by_target = cwd_by_target
        self._pane_cwd_by_session_active = cwd_by_session_active
        self._pane_cache_expires_at = now + self.pane_cache_ttl if self.pane_cache_ttl else 0.0
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0
        return by_target, by_session_active

    def pane_pids(self, records: Sequence[SessionRecord]) -> dict[str, int | None]:
        """Resolve pane pids for many sessions using a short-lived tmux snapshot.

        Supports default ``session:win.pane`` targets and adopted ``%pane_id`` targets.
        """

        out: dict[str, int | None] = {record.id: None for record in records}
        if not records or not self.available:
            return out

        maps = self._load_pane_maps(len(records))
        if maps is None:
            if self._pane_snapshot_timed_out or len(records) > 32:
                # Do not turn one bounded all-pane timeout into N additional
                # synchronous tmux calls. This also applies to a non-timeout
                # failure at fleet scale; recent PIDs are a safer stale view.
                return {record.id: record.last_pid for record in records}
            # Fall back to per-session lookups (same results as before).
            for record in records:
                out[record.id] = self.pane_pid(record)
            return out

        by_target, by_session_active = maps
        for record in records:
            target = self.pane_target(record)
            if target in by_target:
                out[record.id] = by_target[target]
            elif target == f"{record.tmux_name}:0.0":
                out[record.id] = by_session_active.get(record.tmux_name)
            else:
                out[record.id] = None
        return out

    def recovery_metadata(
        self,
        records: Sequence[SessionRecord],
        *,
        bounded: bool = True,
    ) -> dict[str, tuple[str | None, str | None]]:
        """Observe safe crash-relaunch commands and cwd values together.

        Periodic UI refreshes use a bounded round-robin scan. Shutdown and
        explicit checkpoint operations set ``bounded=False`` so every live tab
        gets one final exact-session observation before durable state is saved.
        """

        candidates = [record for record in records if not getattr(record, "external", False)]
        if not candidates:
            return {}
        pids = self.pane_pids(candidates)
        if self._pane_snapshot_stale:
            return {}
        live = [record for record in candidates if pids.get(record.id)]
        if not live:
            return {}
        if bounded:
            start = self._recovery_scan_cursor % len(live)
            count = min(len(live), self.recovery_scan_limit)
            selected = [live[(start + offset) % len(live)] for offset in range(count)]
            self._recovery_scan_cursor = (start + count) % len(live)
        else:
            selected = live
        out: dict[str, tuple[str | None, str | None]] = {}
        for record in selected:
            pid = pids.get(record.id)
            assert pid is not None
            try:
                observed, command, cwd = self._auth_recovery_metadata(record, pid)
            except (OSError, TmuxError, ValueError):
                # Expired/revoked configured authority does not erase the
                # durable conversation or authorize an alternate launch.
                continue
            if observed:
                out[record.id] = (command, cwd if command else None)
        return out

    def recovery_commands(self, records: Sequence[SessionRecord]) -> dict[str, str | None]:
        """Return command-only recovery observations for older callers."""

        return {
            session_id: command
            for session_id, (command, _cwd) in self.recovery_metadata(records).items()
        }

    @staticmethod
    def _private_auth_scope_configured(record: SessionRecord) -> bool:
        return bool(
            (getattr(record, "codex_home", None) or "").strip()
            and (getattr(record, "auth_scope_id", None) or "").strip()
        )

    def _inspect_auth_pane(self, record: SessionRecord) -> _AuthPaneIdentity | None:
        """Read one atomic tmux identity snapshot used by auth preflight."""

        result = self._run(
            [
                "display-message",
                "-p",
                "-t",
                self.pane_target(record),
                "#{session_created}\t#{pane_id}\t#{pane_pid}\t"
                "#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}",
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.rstrip("\n").split("\t", 5)
        if len(parts) != 6:
            return None
        session_created, pane_id, pid_text, current_command, current_path, pane_dead = parts
        if not session_created or not pane_id or pane_dead.strip() not in {"", "0"}:
            return None
        try:
            pane_pid = int(pid_text)
        except ValueError:
            return None
        if pane_pid <= 0 or not current_command.strip():
            return None
        return _AuthPaneIdentity(
            session_created=session_created,
            pane_id=pane_id,
            pane_pid=pane_pid,
            current_command=current_command.strip(),
            current_path=self._normalise_pane_directory(current_path.strip()) or "",
        )

    @classmethod
    def _identity_matches_plan(cls, identity: _AuthPaneIdentity | None, plan: AuthActivationPlan) -> bool:
        if identity is None:
            return False
        process_identity = cls._auth_process_identity(identity.pane_pid)
        return process_identity is not None and process_identity[1] == plan.pane_start_time and (
            identity.session_created,
            identity.pane_id,
            identity.pane_pid,
            identity.current_command,
            identity.current_path,
        ) == (
            plan.session_created,
            plan.pane_id,
            plan.pane_pid,
            plan.pane_current_command,
            plan.pane_current_path,
        )

    @classmethod
    def _idle_shell_executable(cls, current_command: str) -> str | None:
        raw = current_command.strip()
        name = Path(raw).name.casefold().removeprefix("-")
        if name not in cls._SUPPORTED_IDLE_SHELLS:
            return None
        candidate = raw if os.path.sep in raw else name
        resolved = shutil.which(candidate)
        if not resolved:
            return None
        try:
            resolved_path = Path(resolved).resolve(strict=True)
        except OSError:
            return None
        if not resolved_path.is_file() or not os.access(resolved_path, os.X_OK):
            return None
        return str(resolved_path)

    @staticmethod
    def _process_has_live_children(root_pid: int) -> bool | None:
        """Read the kernel's current child list without the sampler cache.

        Auth migration is destructive for a legacy shell, so a briefly stale
        process-tree cache is not sufficient evidence that the shell has no
        background job.  The proc ``children`` files are cheap, per-process,
        and represent the current task tree.  ``None`` deliberately means the
        kernel state could not be read, which callers must treat as unsafe.
        """

        try:
            tids = os.listdir(f"/proc/{root_pid}/task")
        except OSError:
            return None
        saw_children_file = False
        for tid in tids:
            try:
                with open(
                    f"/proc/{root_pid}/task/{tid}/children",
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    children = handle.read().strip()
            except FileNotFoundError:
                continue
            except OSError:
                return None
            saw_children_file = True
            if children:
                return True
        return False if saw_children_file else None

    @classmethod
    def _process_tree_is_idle_shell(cls, root_pid: int) -> bool:
        """Prove the pane shell has no live foreground or background jobs."""

        try:
            # Do the no-cache child check first.  ``process_tree`` has a
            # short fallback cache for dashboard sampling, whereas account
            # migration must never race a just-started background process.
            if cls._process_has_live_children(root_pid) is not False:
                return False
            tree = process_tree(root_pid)
            executable = Path(os.readlink(f"/proc/{root_pid}/exe")).name.casefold()
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            executable in cls._SUPPORTED_IDLE_SHELLS
            and len(tree) == 1
            and tree[0] == root_pid
        )

    def plan_auth_change(self, record: SessionRecord) -> AuthActivationPlan:
        """Collect fail-closed evidence before changing a private auth file.

        A legacy live terminal is eligible only when its process tree is
        readable, contains no Codex process, and the foreground command is a
        supported idle shell.  In that case the plan captures the shell and
        live cwd so apply can recreate the terminal without replaying the
        historical launch command.
        """

        if getattr(record, "external", False):
            return AuthActivationPlan(
                False,
                detail="external tmux sessions cannot be recreated safely",
            )
        if not self._private_auth_scope_configured(record):
            return AuthActivationPlan(
                False,
                detail="terminal has no private Codex auth scope",
            )
        try:
            live = self._auth_session_exists(record)
        except (OSError, TmuxError) as exc:
            return AuthActivationPlan(
                False,
                detail=f"could not prove terminal liveness before account switching: {exc}",
            )
        if not live:
            return AuthActivationPlan(
                True,
                detail="selected account is active for this stopped terminal",
            )

        identity = self._inspect_auth_pane(record)
        if identity is None:
            return AuthActivationPlan(
                False,
                live=True,
                detail="could not bind the live tmux pane before activating the new Codex account",
            )
        process_identity = self._auth_process_identity(identity.pane_pid)
        if process_identity is None:
            return AuthActivationPlan(
                False,
                live=True,
                detail="could not bind the live pane process before activating the new Codex account",
            )
        pane_start_time = process_identity[1]
        environment_ready = self._private_auth_process_environment_ready(
            record,
            identity.pane_pid,
            pane_start_time,
        )
        base = dict(
            live=True,
            environment_ready=environment_ready,
            session_created=identity.session_created,
            pane_id=identity.pane_id,
            pane_pid=identity.pane_pid,
            pane_start_time=pane_start_time,
            pane_current_command=identity.current_command,
            pane_current_path=identity.current_path,
        )

        observed = False
        recovery_command: str | None = None
        recovery_cwd: str | None = None
        inspection_error = ""
        # Codex may rotate its rollout descriptor during a redraw or tool handoff.
        # Retry the now-indexed lookup briefly instead of falling into a manual
        # "next launch" path that contradicts seamless account switching.
        for attempt in range(3):
            try:
                observed, recovery_command, recovery_cwd = self._auth_recovery_metadata(
                    record, identity.pane_pid
                )
                inspection_error = ""
            except (OSError, RuntimeError, ValueError) as exc:
                observed, recovery_command, recovery_cwd = False, None, None
                inspection_error = str(exc)
            if observed and recovery_command:
                candidate = replace(
                    record,
                    recovery_command=recovery_command,
                    recovery_cwd=recovery_cwd,
                )
                exact_recovery = self._exact_auth_recovery(candidate)
                if exact_recovery is not None:
                    return AuthActivationPlan(
                        True,
                        restart=True,
                        recovery_command=exact_recovery,
                        recovery_cwd=recovery_cwd,
                        **base,
                        detail="exact Codex conversation is ready for seamless account handoff",
                    )
            if attempt < 2:
                time.sleep(0.025 * (attempt + 1))

        if record.tag == "web-agent":
            suffix = f": {inspection_error}" if inspection_error else ""
            return AuthActivationPlan(
                False,
                **base,
                detail=f"configured task requires its exact authorized Codex conversation{suffix}",
            )
        codex_state = process_tree_contains_codex(identity.pane_pid)
        current_name = Path(identity.current_command).name.casefold().removeprefix("-")
        if (
            codex_state is not False
            or current_name == "codex"
            or current_name.startswith("codex-")
        ):
            suffix = f": {inspection_error}" if inspection_error else ""
            return AuthActivationPlan(
                False,
                **base,
                detail=(
                    "running Codex conversation could not be bound to an exact seamless "
                    f"resume identity; retry the account selection{suffix}"
                ),
            )

        if environment_ready:
            return AuthActivationPlan(
                True,
                **base,
                detail="selected account is active; this private terminal is ready",
            )
        replacement_command = self._idle_shell_executable(identity.current_command)
        if replacement_command is None:
            return AuthActivationPlan(
                False,
                **base,
                detail=(
                    "legacy terminal is not at a verifiable idle shell; "
                    "exit the foreground program before switching accounts"
                ),
            )
        if Path(replacement_command).name.casefold() != "bash":
            return AuthActivationPlan(
                False,
                **base,
                detail=(
                    "legacy managed-shell account migration currently supports Bash only; "
                    "the running terminal was left unchanged"
                ),
            )
        if not self._process_tree_is_idle_shell(identity.pane_pid):
            return AuthActivationPlan(
                False,
                **base,
                detail=(
                    "legacy shell still has a running foreground or background job; "
                    "stop it before switching accounts"
                ),
            )
        if not identity.current_path:
            return AuthActivationPlan(
                False,
                **base,
                detail="could not capture the legacy terminal's live working directory",
            )
        return AuthActivationPlan(
            True,
            restart=True,
            replacement_command=replacement_command,
            replacement_cwd=identity.current_path,
            **base,
            detail="legacy terminal is ready to restart once with private Codex auth",
        )

    @staticmethod
    def _auth_plan_token(plan: AuthActivationPlan) -> tuple[object, ...]:
        return (
            plan.ok,
            plan.live,
            plan.restart,
            plan.environment_ready,
            plan.session_created,
            plan.pane_id,
            plan.pane_pid,
            plan.pane_start_time,
            plan.pane_current_command,
            plan.pane_current_path,
            plan.replacement_command,
            plan.replacement_cwd,
            plan.recovery_command,
            plan.recovery_cwd,
        )

    @staticmethod
    def _auth_process_identity(pid: int) -> tuple[str, int] | None:
        """Return Linux process state and start time without trusting PID reuse."""

        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            return None
        end = raw.rfind(")")
        if end < 0:
            return None
        fields = raw[end + 2 :].split()
        if len(fields) <= 19:
            return None
        try:
            return fields[0], int(fields[19])
        except (TypeError, ValueError):
            return None

    @classmethod
    def _resume_auth_processes(
        cls,
        processes: tuple[tuple[int, int, bool], ...],
    ) -> None:
        """Resume only processes this transaction stopped and whose PID is unchanged."""

        errors: list[str] = []
        resumed: list[tuple[int, int]] = []
        for pid, start_time, was_stopped in reversed(processes):
            if was_stopped:
                continue
            identity = cls._auth_process_identity(pid)
            if identity is None:
                continue
            _state, current_start = identity
            if current_start != start_time:
                errors.append(f"process identity changed while resuming pid {pid}")
                continue
            try:
                os.kill(pid, signal.SIGCONT)
                resumed.append((pid, start_time))
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                errors.append(f"permission denied while resuming auth process pid {pid}: {exc}")
            except OSError as exc:
                errors.append(f"could not resume auth process pid {pid}: {exc}")
        if not errors:
            return

        # SIGCONT is not transactional. If any member could not be resumed,
        # put every member that *was* resumed back into the verified stopped
        # state before reporting failure. This prevents a partially running
        # tree from consuming a pointer that the store records as paused.
        restop_errors: list[str] = []
        for pid, start_time in reversed(resumed):
            identity = cls._auth_process_identity(pid)
            if identity is None or identity[1] != start_time:
                continue
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                continue
            except OSError as exc:
                restop_errors.append(f"could not re-stop auth process pid {pid}: {exc}")
        deadline = time.monotonic() + 0.5
        still_running: list[int] = []
        while True:
            still_running = []
            for pid, start_time in resumed:
                identity = cls._auth_process_identity(pid)
                if (
                    identity is not None
                    and identity[1] == start_time
                    and identity[0] not in {"T", "t"}
                ):
                    still_running.append(pid)
            if not still_running or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if still_running:
            restop_errors.append(
                "auth processes remained running after resume rollback: "
                + ", ".join(str(pid) for pid in still_running)
            )
        if restop_errors:
            raise _AuthProcessesMayRemainStopped(
                "; ".join([*errors, *restop_errors]),
                processes,
            )
        raise TmuxError("; ".join(errors))

    @classmethod
    def _suspend_auth_process_tree(
        cls,
        root_pid: int,
        *,
        allow_running_root: bool = False,
    ) -> tuple[tuple[int, int, bool], ...]:
        """Stop a stable pane process tree, closing the launch-during-switch race.

        tmux deliberately sends ``SIGCONT`` when its direct pane child stops.
        Once tmux input has been disabled for that pane, callers may therefore
        allow only that direct child to remain running while every descendant
        is stopped.  The root is either an idle shell or is removed before any
        credential pointer changes; a running descendant is never accepted.
        """

        observed: dict[int, tuple[int, bool]] = {}
        try:
            for _attempt in range(4):
                tree = process_tree(root_pid)
                if not tree or root_pid not in tree:
                    raise TmuxError("live auth process tree disappeared before it could be quiesced")
                for pid in tree:
                    identity = cls._auth_process_identity(pid)
                    if identity is None:
                        if pid == root_pid:
                            raise TmuxError("live auth pane process became unreadable")
                        continue
                    state, start_time = identity
                    previous = observed.get(pid)
                    if previous is not None and previous[0] != start_time:
                        raise TmuxError(f"process identity changed while quiescing pid {pid}")
                    observed.setdefault(pid, (start_time, state in {"T", "t"}))
                for pid in reversed(tree):
                    identity = cls._auth_process_identity(pid)
                    if identity is None:
                        continue
                    state, start_time = identity
                    expected = observed.get(pid)
                    if expected is None or expected[0] != start_time:
                        raise TmuxError(f"process identity changed while quiescing pid {pid}")
                    if state not in {"T", "t"}:
                        os.kill(pid, signal.SIGSTOP)

                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    current = process_tree(root_pid)
                    if not current:
                        raise TmuxError("live auth pane exited while being quiesced")
                    identities = {
                        pid: cls._auth_process_identity(pid)
                        for pid in current
                    }
                    if all(
                        identity is not None
                        and (
                            identity[0] in {"T", "t"}
                            or (allow_running_root and pid == root_pid)
                        )
                        for pid, identity in identities.items()
                    ):
                        if set(current).issubset(observed):
                            return tuple(
                                (pid, start_time, was_stopped)
                                for pid, (start_time, was_stopped) in sorted(observed.items())
                            )
                        break
                    time.sleep(0.01)
            raise TmuxError("live auth process tree did not stop completely")
        except (OSError, TmuxError) as exc:
            snapshot = tuple(
                (pid, start_time, was_stopped)
                for pid, (start_time, was_stopped) in sorted(observed.items())
            )
            try:
                cls._resume_auth_processes(snapshot)
            except (OSError, TmuxError) as resume_exc:
                raise _AuthProcessesMayRemainStopped(
                    f"{exc}; could not restore every stopped process: {resume_exc}",
                    snapshot,
                ) from exc
            if isinstance(exc, TmuxError):
                raise
            raise TmuxError(f"could not quiesce live auth process tree: {exc}") from exc

    def _disable_auth_pane_input(self, pane_id: str) -> bool:
        """Disable tmux input and return whether the pane was already disabled."""

        state = self._run(
            ["display-message", "-p", "-t", pane_id, "#{pane_input_off}"],
            check=False,
            timeout=self.query_timeout,
        )
        if state.returncode != 0 or state.stdout.strip() not in {"0", "1"}:
            raise TmuxError(
                state.stderr.strip() or "could not inspect terminal input state"
            )
        was_off = state.stdout.strip() == "1"
        if not was_off:
            changed = self._run(
                ["select-pane", "-d", "-t", pane_id],
                check=False,
                timeout=self.query_timeout,
            )
            if changed.returncode != 0:
                raise TmuxError(
                    changed.stderr.strip() or "could not disable terminal input"
                )
        verified = self._run(
            ["display-message", "-p", "-t", pane_id, "#{pane_input_off}"],
            check=False,
            timeout=self.query_timeout,
        )
        if verified.returncode != 0 or verified.stdout.strip() != "1":
            raise TmuxError(
                verified.stderr.strip() or "terminal input did not become disabled"
            )
        return was_off

    def _restore_auth_pane_input(self, pane_id: str, was_off: bool) -> None:
        """Restore the exact pre-transaction tmux input state."""

        if was_off:
            return
        changed = self._run(
            ["select-pane", "-e", "-t", pane_id],
            check=False,
            timeout=self.query_timeout,
        )
        if changed.returncode != 0:
            raise TmuxError(
                changed.stderr.strip() or "could not restore terminal input"
            )
        verified = self._run(
            ["display-message", "-p", "-t", pane_id, "#{pane_input_off}"],
            check=False,
            timeout=self.query_timeout,
        )
        if verified.returncode != 0 or verified.stdout.strip() != "0":
            raise TmuxError(
                verified.stderr.strip() or "terminal input remained disabled"
            )

    def _restore_quiesced_auth_pane(
        self,
        pane_id: str,
        input_was_off: bool,
        processes: tuple[tuple[int, int, bool], ...],
    ) -> None:
        """Resume a failed auth quiescence and restore its prior input state."""

        errors: list[str] = []
        try:
            self._resume_auth_processes(processes)
        except (OSError, TmuxError) as exc:
            errors.append(str(exc))
        try:
            self._restore_auth_pane_input(pane_id, input_was_off)
        except (OSError, TmuxError) as exc:
            errors.append(str(exc))
        if errors:
            raise TmuxError("; ".join(errors))

    def _terminate_auth_session(self, record: SessionRecord) -> None:
        """Synchronously remove one prevalidated tmux session for auth switching."""

        with self._history_session_lock_for(record.id):
            self._history_pop(record.id)
        result = self._run(
            ["kill-session", "-t", record.tmux_name],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0 and self.session_exists(record):
            raise TmuxError(result.stderr.strip() or "tmux session could not be quiesced")
        self.checkpoints.clear(record)
        self.invalidate_cache()

    @classmethod
    def _kill_quiesced_auth_processes(
        cls,
        processes: tuple[tuple[int, int, bool], ...],
    ) -> None:
        """Ensure a removed tmux session cannot retain a detached auth writer."""

        errors: list[str] = []
        for pid, start_time, _was_stopped in reversed(processes):
            identity = cls._auth_process_identity(pid)
            if identity is None:
                continue
            _state, current_start = identity
            if current_start != start_time:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError as exc:
                errors.append(f"could not terminate old auth process pid {pid}: {exc}")

        deadline = time.monotonic() + 0.75
        survivors: list[int] = []
        while True:
            survivors = []
            for pid, start_time, _was_stopped in processes:
                identity = cls._auth_process_identity(pid)
                if identity is not None and identity[1] == start_time:
                    survivors.append(pid)
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if survivors:
            errors.append(
                "old auth process did not exit: " + ", ".join(str(pid) for pid in survivors)
            )
        if errors:
            raise TmuxError("; ".join(errors))

    @classmethod
    def _snapshot_auth_process_tree(
        cls,
        root_pid: int,
    ) -> tuple[tuple[int, int, bool], ...]:
        """Capture a stable process identity set without sending any signal."""

        for _attempt in range(4):
            before = process_tree(root_pid)
            if not before or root_pid not in before:
                raise TmuxError("live auth process tree disappeared before handoff")
            rows: list[tuple[int, int, bool]] = []
            complete = True
            for pid in before:
                identity = cls._auth_process_identity(pid)
                if identity is None:
                    complete = False
                    break
                state, start_time = identity
                rows.append((pid, start_time, state in {"T", "t"}))
            after = process_tree(root_pid)
            if complete and before == after:
                return tuple(rows)
            time.sleep(0.01)
        raise TmuxError("live auth process tree changed while preparing seamless handoff")

    @staticmethod
    def _auth_switch_gate_path(record: SessionRecord) -> Path:
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        if not raw_home:
            raise TmuxError("terminal has no private Codex auth home")
        root = Path(raw_home).expanduser().resolve(strict=False) / ".termdeck-auth-switch-gates"
        _ensure_auth_lifetime_root(root)
        return root / (
            f"gate-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
        )

    @staticmethod
    def _open_auth_switch_gate(path: Path) -> None:
        """Open one owner-only, no-follow, one-shot launch gate."""

        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise TmuxError("this platform cannot open the Codex handoff gate safely")
            fd = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | nofollow,
                0o600,
            )
        except FileExistsError:
            metadata = path.lstat()
            if (
                not stat_module.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat_module.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise TmuxError("Codex handoff gate already exists with unsafe metadata")
            return
        except TmuxError:
            raise
        except OSError as exc:
            raise TmuxError(f"could not open the Codex handoff gate: {exc}") from exc
        else:
            os.close(fd)

    @staticmethod
    def _cleanup_auth_switch_gate(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _cancel_confined_resume(record: SessionRecord, spec: _LaunchSpec | None) -> None:
        if record.tag != "web-agent":
            return
        permit = getattr(spec, "confined_resume_permit", None)
        if isinstance(permit, str) and permit:
            from .agent_launch import cancel_resume
            cancel_resume(record, permit)

    @staticmethod
    def _gated_auth_launch_command(spec: _LaunchSpec, gate_path: Path) -> str:
        gate = shlex.quote(str(gate_path))
        payload = shlex.quote(spec.command)
        script = (
            "set -eu\n"
            f"while [ ! -e {gate} ]; do sleep 0.02; done\n"
            f"rm -f -- {gate}\n"
            f"exec /bin/bash --noprofile --norc -c {payload}"
        )
        return shlex.join(["/bin/bash", "--noprofile", "--norc", "-c", script])

    @staticmethod
    def _auth_replacement_record(
        record: SessionRecord,
        plan: AuthActivationPlan,
    ) -> SessionRecord:
        if plan.recovery_command is not None:
            return replace(
                record,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                status="running",
            )
        if not plan.replacement_command or not plan.replacement_cwd:
            raise TmuxError("account handoff plan has no replacement payload")
        return replace(
            record,
            command=plan.replacement_command,
            cwd=plan.replacement_cwd,
            recovery_command=None,
            recovery_cwd=None,
            status="running",
        )

    def _respawn_auth_pane_gated(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
        spec: _LaunchSpec,
        gate_path: Path,
    ) -> None:
        if plan.pane_id is None:
            raise TmuxError("account handoff lost the stable tmux pane id")
        command = self._gated_auth_launch_command(spec, gate_path)
        args = ["respawn-pane", "-k", "-t", plan.pane_id]
        args.extend(self._environment_args(spec.environment))
        args.extend(["-c", str(spec.directory), command])
        result = self._run(args, check=False, timeout=15.0)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "could not replace the pane in place")
        self._run(
            ["set-option", "-t", record.tmux_name, "remain-on-exit", "on"],
            check=False,
        )
        self.invalidate_cache()

    def _resume_gated_auth_handoff(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
        *,
        gate_path: Path,
        input_was_off: bool,
        timeout: float = 15.0,
    ) -> tuple[_AuthPaneIdentity | None, str]:
        self._open_auth_switch_gate(gate_path)
        identity, detail = self._await_auth_recreation_ready(
            record,
            previous_session_created=plan.session_created,
            previous_pane_id=plan.pane_id,
            previous_pane_pid=plan.pane_pid,
            previous_pane_start_time=plan.pane_start_time,
            expected_recovery_command=plan.recovery_command,
            expected_recovery_cwd=plan.recovery_cwd,
            timeout=timeout,
        )
        if plan.pane_id is not None:
            self._restore_auth_pane_input(plan.pane_id, input_was_off)
        self._cleanup_auth_switch_gate(gate_path)
        return identity, detail

    def quiesce_auth_change(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
    ) -> AuthQuiesceResult:
        """Prepare an atomic account handoff without pausing or deleting the tab.

        Restartable Codex sessions are replaced *in the same tmux pane* by a
        private-environment launcher blocked on an owner-only gate.  The old
        process tree is gone before the canonical auth pointer changes; opening
        the gate after that change resumes the exact conversation.  Idle private
        shells need only a short input fence because no Codex process consumes
        auth while the pointer changes.
        """

        token = self._auth_plan_token(plan)
        if not plan.ok:
            return AuthQuiesceResult(False, plan_token=token, detail=plan.detail)
        if getattr(record, "external", False) or not self._private_auth_scope_configured(record):
            return AuthQuiesceResult(
                False,
                plan_token=token,
                detail="terminal auth scope changed after preflight; no credential was changed",
            )
        if not plan.live:
            try:
                live = self._auth_session_exists(record)
            except (OSError, TmuxError) as exc:
                return AuthQuiesceResult(
                    False,
                    plan_token=token,
                    detail=f"could not verify the terminal stayed stopped: {exc}",
                )
            if live:
                return AuthQuiesceResult(
                    False,
                    plan_token=token,
                    detail="terminal started after auth preflight; no credential was changed",
                )
            try:
                lifetime_lease = _acquire_auth_lifetime_lease(record)
            except (OSError, TmuxError) as exc:
                return AuthQuiesceResult(
                    False,
                    plan_token=token,
                    detail=f"could not prove the stopped auth scope has no live owner: {exc}",
                )
            return AuthQuiesceResult(
                True,
                plan_token=token,
                detail="selected account is active for this terminal",
                lifetime_lease=lifetime_lease,
            )

        current_identity = self._inspect_auth_pane(record)
        if not self._identity_matches_plan(current_identity, plan):
            return AuthQuiesceResult(
                False,
                live=True,
                plan_token=token,
                detail="live tmux pane changed after auth preflight; no process was changed",
            )
        if plan.pane_id is None or plan.pane_pid is None:
            return AuthQuiesceResult(
                False,
                live=True,
                plan_token=token,
                detail="live terminal lost its stable pane identity",
            )
        try:
            input_was_off = self._disable_auth_pane_input(plan.pane_id)
        except (OSError, TmuxError) as exc:
            return AuthQuiesceResult(
                False,
                live=True,
                plan_token=token,
                detail=f"could not fence terminal input for account switching: {exc}",
            )

        if not plan.restart:
            try:
                if not self._identity_matches_plan(self._inspect_auth_pane(record), plan):
                    raise TmuxError("live tmux pane changed while input was fenced")
                if process_tree_contains_codex(plan.pane_pid) is not False:
                    raise TmuxError("a Codex process appeared during account handoff")
                if not self._private_auth_process_tree_environment_ready(record, plan.pane_pid):
                    raise TmuxError("terminal no longer has its private auth environment")
            except (OSError, TmuxError) as exc:
                try:
                    self._restore_auth_pane_input(plan.pane_id, input_was_off)
                except (OSError, TmuxError) as restore_exc:
                    return AuthQuiesceResult(
                        False,
                        live=True,
                        plan_token=token,
                        input_pane_id=plan.pane_id,
                        input_was_off=input_was_off,
                        detail=f"{exc}; terminal input could not be restored: {restore_exc}",
                    )
                return AuthQuiesceResult(False, live=True, plan_token=token, detail=str(exc))
            return AuthQuiesceResult(
                True,
                live=True,
                plan_token=token,
                input_pane_id=plan.pane_id,
                input_was_off=input_was_off,
                detail="selected account is active; terminal remains ready",
            )

        gate_path: Path | None = None
        replacement_record: SessionRecord | None = None
        launch_spec: _LaunchSpec | None = None
        old_processes: tuple[tuple[int, int, bool], ...] = ()
        respawned = False
        try:
            if plan.recovery_command is not None:
                observed, recovery_command, recovery_cwd = self._auth_recovery_metadata(
                    record, plan.pane_pid
                )
                if (
                    not observed
                    or recovery_command != plan.recovery_command
                    or recovery_cwd != plan.recovery_cwd
                ):
                    raise TmuxError("exact Codex recovery identity changed after preflight")
            elif not self._process_tree_is_idle_shell(plan.pane_pid):
                raise TmuxError("idle shell started a job after account preflight")

            old_processes = self._snapshot_auth_process_tree(plan.pane_pid)
            replacement_record = self._auth_replacement_record(record, plan)
            launch_spec = self._launch_spec(replacement_record)
            gate_path = self._auth_switch_gate_path(record)
            self._respawn_auth_pane_gated(
                replacement_record,
                plan,
                launch_spec,
                gate_path,
            )
            respawned = True
            placeholder, detail = self._await_auth_recreation_ready(
                replacement_record,
                previous_session_created=plan.session_created,
                previous_pane_id=plan.pane_id,
                previous_pane_pid=plan.pane_pid,
                previous_pane_start_time=plan.pane_start_time,
                expected_recovery_command=None,
                expected_recovery_cwd=None,
                timeout=4.0,
            )
            if placeholder is None or detail:
                raise TmuxError(detail or "in-place auth handoff launcher was not ready")
            self._kill_quiesced_auth_processes(old_processes)
            lifetime_lease = _acquire_auth_lifetime_lease(record)
        except (OSError, TmuxError) as exc:
            recovery_error = ""
            recovered_identity: _AuthPaneIdentity | None = None
            recovered_payload = False
            if respawned and gate_path is not None and replacement_record is not None:
                try:
                    self._open_auth_switch_gate(gate_path)
                    recovered_identity, recovery_detail = self._await_auth_recreation_ready(
                        replacement_record,
                        previous_session_created=plan.session_created,
                        previous_pane_id=plan.pane_id,
                        previous_pane_pid=plan.pane_pid,
                        previous_pane_start_time=plan.pane_start_time,
                        expected_recovery_command=plan.recovery_command,
                        expected_recovery_cwd=plan.recovery_cwd,
                        timeout=8.0,
                    )
                    if recovered_identity is None or recovery_detail:
                        recovery_error = (
                            "; old account handoff recovery failed: "
                            + (recovery_detail or "exact previous payload was not ready")
                        )
                    else:
                        recovered_payload = True
                except (OSError, TmuxError) as recover_exc:
                    recovery_error = f"; old account handoff recovery failed: {recover_exc}"
            try:
                self._restore_auth_pane_input(plan.pane_id, input_was_off)
            except (OSError, TmuxError) as input_exc:
                recovered_payload = False
                recovery_error += f"; terminal input could not be restored: {input_exc}"
            self._cleanup_auth_switch_gate(gate_path)
            self._cancel_confined_resume(record, launch_spec)
            return AuthQuiesceResult(
                False,
                live=True,
                plan_token=token,
                input_pane_id=plan.pane_id,
                input_was_off=input_was_off,
                gated_respawn=respawned,
                recovered_payload=recovered_payload,
                current_pid=(
                    recovered_identity.pane_pid
                    if recovered_payload and recovered_identity is not None
                    else None
                ),
                detail=f"seamless account handoff could not be prepared: {exc}{recovery_error}",
            )

        return AuthQuiesceResult(
            True,
            live=True,
            plan_token=token,
            suspended_processes=old_processes,
            input_pane_id=plan.pane_id,
            input_was_off=input_was_off,
            gated_respawn=True,
            current_pid=placeholder.pane_pid,
            gate_path=str(gate_path),
            replacement_record=replacement_record,
            launch_spec=launch_spec,
            detail="exact Codex session is ready for in-place account handoff",
            lifetime_lease=lifetime_lease,
        )

    @staticmethod
    def _blocked_auth_activation(
        plan: AuthActivationPlan,
        quiescence: AuthQuiesceResult,
        detail: str,
    ) -> AuthActivationResult:
        if quiescence.gated_respawn:
            return AuthActivationResult(
                False,
                False,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                pid=(
                    quiescence.current_pid
                    if quiescence.recovered_payload
                    else None
                ),
                status=(
                    "running"
                    if quiescence.recovered_payload
                    else "recovery-pending"
                ),
                detail=detail,
                preserve_selected_account=True,
                command=plan.replacement_command,
                cwd=plan.replacement_cwd,
                clear_recovery=plan.recovery_command is None,
            )
        if quiescence.terminated:
            return AuthActivationResult(
                False,
                False,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                status="recovery-pending" if plan.recovery_command else "stopped",
                detail=detail,
                preserve_selected_account=True,
                command=plan.replacement_command,
                cwd=plan.replacement_cwd,
                clear_recovery=plan.recovery_command is None,
            )
        if quiescence.suspended:
            return AuthActivationResult(
                False,
                False,
                pid=plan.pane_pid,
                status="paused",
                detail=detail,
                preserve_selected_account=True,
            )
        return AuthActivationResult(
            False,
            False,
            pid=plan.pane_pid if plan.live else None,
            status="running" if plan.live else None,
            detail=detail,
            preserve_selected_account=True,
        )

    def _cleanup_failed_auth_recreation(
        self,
        record: SessionRecord,
        identity: _AuthPaneIdentity | None,
        *,
        timeout: float = 1.5,
    ) -> tuple[bool, str]:
        """Remove only the freshly observed session and prove it disappeared.

        ``kill-session`` is synchronous for tmux's registry, but the pane child
        can take a few scheduler turns to consume SIGHUP and be reaped.  Bind to
        its kernel start time before mutation, then wait boundedly for that exact
        process to disappear, become a zombie, or be replaced by PID reuse.
        """

        if identity is None:
            return False, "fresh terminal identity was never established"
        process_before = self._auth_process_identity(identity.pane_pid)
        if process_before is None:
            return False, "fresh terminal process identity was unavailable before cleanup"
        try:
            current = self._inspect_auth_pane(record)
        except (OSError, TmuxError) as exc:
            return False, f"could not revalidate the failed terminal: {exc}"
        if current is not None and current != identity:
            return False, "terminal identity changed before failed-restart cleanup"
        try:
            self._terminate_auth_session(record)
        except (OSError, TmuxError) as exc:
            return False, f"could not remove the failed terminal: {exc}"
        try:
            if self._auth_session_exists(record):
                return False, "failed terminal remained live after cleanup"
        except (OSError, TmuxError) as exc:
            return False, f"could not prove the failed terminal exited: {exc}"

        expected_start_time = process_before[1]
        deadline = time.monotonic() + max(0.05, float(timeout))
        while True:
            process_after = self._auth_process_identity(identity.pane_pid)
            if (
                process_after is None
                or process_after[1] != expected_start_time
                or process_after[0] == "Z"
            ):
                return True, ""
            if time.monotonic() >= deadline:
                return False, "failed terminal process remained live after cleanup"
            time.sleep(0.025)

    def take_auth_readiness_diagnostics(self, session_id: str) -> list[dict[str, object]]:
        """Consume only this tab's bounded, secret-free failed-readiness evidence."""
        return getattr(self, "_auth_readiness_failures", {}).pop(session_id, [])

    def _auth_readiness_process_diagnostic(
        self, record: SessionRecord, pid: int, expected_start: int,
    ) -> dict[str, object]:
        """Report equality facts only; never return environment or command values."""
        result: dict[str, object] = {"pid": pid, "start_time": expected_start,
            "process_state": "unavailable", "environment_read": "not-read",
            "home_matches": None, "scope_matches": None,
            "gate_matches": None, "path_first_matches": None}
        before = self._auth_process_identity(pid)
        if before is None:
            try:
                Path(f"/proc/{pid}").stat()
            except FileNotFoundError:
                result["process_state"] = "exited"
            except OSError:
                pass
            return result
        if before[1] != expected_start:
            result["process_state"] = "incarnation-changed"
            return result
        if before[0] in {"Z", "X", "x"}:
            result["process_state"] = "exited"
            return result
        result["process_state"] = "stopped" if before[0] in {"T", "t"} else "live"
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except OSError:
            result["environment_read"] = "unavailable"
            return result
        if len(raw) > 1024 * 1024:
            result["environment_read"] = "too-large"
            return result
        wanted = {b"CODEX_HOME", b"TERMDECK_AUTH_SCOPE_ID", b"CODEX_TERMDECK_GATE_ROOT", b"PATH"}
        environment = {}
        for entry in raw.split(b"\0"):
            key, separator, value = entry.partition(b"=")
            if separator and key in wanted:
                environment[key] = os.fsdecode(value)
        after = self._auth_process_identity(pid)
        if after is None or after[1] != expected_start:
            result["process_state"] = "unavailable" if after is None else "incarnation-changed"
            if after is None:
                try:
                    Path(f"/proc/{pid}").stat()
                except FileNotFoundError:
                    result["process_state"] = "exited"
                except OSError:
                    pass
            result["environment_read"] = "identity-ambiguous"
            return result
        if after[0] in {"Z", "X", "x"}:
            result["process_state"] = "exited"
            result["environment_read"] = "identity-ambiguous"
            return result
        home = str(Path(record.codex_home or "").expanduser().resolve(strict=False))
        result.update(environment_read="observed",
            home_matches=environment.get(b"CODEX_HOME") == home,
            scope_matches=environment.get(b"TERMDECK_AUTH_SCOPE_ID") == record.auth_scope_id,
            gate_matches=environment.get(b"CODEX_TERMDECK_GATE_ROOT") == str(auth_lifetime_gate_root(record)),
            path_first_matches=environment.get(b"PATH", "").split(os.pathsep)[0] == str(Path(home) / "bin"))
        return result


    @staticmethod
    def _auth_readiness_entrypoint_category(raw: str, exit_status: int | None) -> str:
        """Classify closed public literals only; output text has no authority."""
        categories = set()
        if exit_status == 78:
            prefix = "TermDeck native task could not start: "
            messages = {
                "Launch is unavailable": "launch-authority",
                "Launch permit was already consumed": "launch-authority",
                "Launch policy changed": "policy-authority",
                "Configured launch policy changed": "policy-authority",
                "Resume permit is unavailable or consumed": "resume-authority",
                "Resume permit was already consumed": "resume-authority",
                "Private account binding needs repair": "private-account-binding",
                "Resume private scope changed": "private-scope",
                "Configured private scope changed": "private-scope",
                "Private native configuration has no validated provenance": "private-configuration",
                "Private native configuration authority cannot be validated": "private-configuration",
                "Shared asset catalogue cannot be validated": "shared-assets",
                "Shared asset binding changed": "shared-assets",
                "Configured launch binding changed": "configured-binding",
                "Configured tab is unavailable": "configured-binding",
                "Configured tab binding changed": "configured-binding",
                "Configured launcher identity changed": "configured-binding",
                "Tab is not authorized": "configured-binding",
                "Configured account source changed": "configured-binding",
                "Configured Codex runtime changed": "configured-binding",
                "Original launch machine was revoked; authorize a new launch": "original-launch-auth",
                "Original launch login expired; authorize a new launch with the current login": "original-launch-auth",
                "Original launch login was revoked; authorize a new launch with the current login": "original-launch-auth",
                "Original launch authorization changed; authorize a new launch": "original-launch-auth",
                "Native executable binding is invalid": "native-authority",
                "Native executable authority is unsafe": "native-authority",
                "Native executable changed while opening": "native-authority",
                "Qualified Codex executable changed before launch": "native-authority",
                "Native executable changed during verification": "native-authority",
            }
            generic = "TermDeck native task could not start; review its launch policy and attachment authorization."
            for line in raw.splitlines():
                if line.startswith(prefix):
                    categories.add(messages.get(line[len(prefix):], "unknown"))
                elif line == generic:
                    categories.add("entrypoint-io-or-value-refusal")
        elif exit_status == 1 and "Traceback (most recent call last):" in raw.splitlines():
            exceptions = {"KeyError": "python-key-error", "AttributeError": "python-attribute-error",
                "TypeError": "python-type-error", "NameError": "python-name-error",
                "RuntimeError": "python-runtime-error", "ImportError": "python-import-error",
                "ModuleNotFoundError": "python-import-error", "MemoryError": "python-memory-error",
                "ValueError": "python-value-error", "OSError": "python-os-error",
                "FileNotFoundError": "python-os-error", "PermissionError": "python-os-error"}
            for line in raw.splitlines():
                kind, separator, _private_message = line.partition(":")
                if separator and kind in exceptions:
                    categories.add(exceptions[kind])
        return next(iter(categories)) if len(categories) == 1 else "unknown"

    def _auth_readiness_exit_diagnostic(
        self, record: SessionRecord, identity: _AuthPaneIdentity | None, start_time: int | None,
    ) -> dict[str, object]:
        """Observe only the failed managed pane; never retain its captured text."""
        result: dict[str, object] = {"pane_identity": "unobserved", "dead_pane": None,
            "exit_status": None, "exit_signal": None, "category_read": "not-read",
            "entrypoint_error_category": "unknown", "pane_read": "not-read",
            "cwd_read": "not-read", "last_live_cwd_matches": None}
        if (record.external or record.tag != "web-agent" or not self._private_auth_scope_configured(record)
                or identity is None or start_time is None or not re.fullmatch(r"%[0-9]+", identity.pane_id)):
            return result
        expected_cwd = self._normalise_pane_directory(record.cwd)
        result["last_live_cwd_matches"] = bool(expected_cwd
            and self._normalise_pane_directory(identity.current_path) == expected_cwd)
        if not result["last_live_cwd_matches"]:
            result.update(pane_identity="ambiguous", pane_read="last-live-cwd-mismatch")
            return result
        timeout = min(0.25, self.query_timeout)
        format_text = ("#{session_name}\t#{session_created}\t#{pane_id}\t#{pane_pid}\t"
            "#{pane_current_path}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_dead_signal}\t#{pane_width}")
        def read_pane():
            response = self._run(["display-message", "-p", "-t", self.pane_target(record), format_text],
                check=False, timeout=timeout)
            if response.returncode != 0:
                result["pane_read"] = "query-failed"
                return None
            fields = response.stdout.rstrip("\n").split("\t")
            if len(fields) != 9:
                result["pane_read"] = "format-invalid"
                return None
            if fields[:2] != [record.tmux_name, identity.session_created]:
                result["pane_read"] = "session-mismatch"
                return None
            if fields[2] != identity.pane_id:
                result["pane_read"] = "pane-mismatch"
                return None
            if fields[3] != str(identity.pane_pid):
                result["pane_read"] = "pid-mismatch"
                return None
            if fields[5] not in {"0", "1"}:
                result["pane_read"] = "dead-state-invalid"
                return None
            if not re.fullmatch(r"[0-9]{1,4}", fields[8]) or not 1 <= int(fields[8]) <= 1000:
                result["pane_read"] = "width-invalid"
                return None
            if fields[4] == "" and fields[5] == "1":
                # tmux closes the retained dead pane's FD: current_path is unavailable.
                # The exact last live cwd above, pane/PID binding and double read remain required.
                result["cwd_read"] = "unavailable-after-exit"
            elif self._normalise_pane_directory(fields[4]) != expected_cwd:
                result.update(pane_read="cwd-mismatch", cwd_read="mismatch")
                return None
            else:
                result["cwd_read"] = "matched"
            result["pane_read"] = "observed"
            return fields
        def exited_or_absent():
            process = self._auth_process_identity(identity.pane_pid)
            if process is not None:
                return process[1] == start_time and process[0] in {"Z", "X", "x"}
            try:
                Path(f"/proc/{identity.pane_pid}").stat()
            except FileNotFoundError:
                return True
            except OSError:
                pass
            return False
        before = read_pane()
        if before is None:
            result["pane_identity"] = "ambiguous"
            return result
        if before[5] == "0":
            result.update(pane_identity="bound", dead_pane=False)
            return result
        status, dead_signal = None, None
        if before[6]:
            if not re.fullmatch(r"[0-9]{1,3}", before[6]) or int(before[6]) > 255:
                result.update(pane_identity="ambiguous", pane_read="exit-status-invalid")
                return result
            status = int(before[6])
        if before[7]:
            names = {name.removeprefix("SIG"): int(value)
                for name, value in signal.Signals.__members__.items()}
            dead_signal = names.get(before[7])
            if dead_signal is None:
                result.update(pane_identity="ambiguous", pane_read="exit-signal-invalid")
                return result
        if status is not None and dead_signal is not None:
            result.update(pane_identity="ambiguous", pane_read="exit-ambiguous")
            return result
        if not exited_or_absent():
            result.update(pane_identity="ambiguous", pane_read="process-ambiguous")
            return result
        category, category_read = "unknown", "not-read"
        if status in {1, 78} and dead_signal is None:
            captured = self._run(["capture-pane", "-p", "-t", identity.pane_id, "-S", "-32", "-E", "32"],
                check=False, timeout=timeout)
            if captured.returncode != 0:
                category_read = "unavailable"
            elif len(captured.stdout.encode("utf-8")) > 65536:
                category_read = "too-large"
            else:
                category_read = "observed"
                category = self._auth_readiness_entrypoint_category(captured.stdout, status)
        after = read_pane()
        if after != before:
            result["pane_identity"] = "ambiguous"
            if after is not None:
                result["pane_read"] = "pane-changed"
            return result
        if not exited_or_absent():
            result.update(pane_identity="ambiguous", pane_read="process-ambiguous")
            return result
        result.update(pane_identity="bound", dead_pane=True, exit_status=status,
            exit_signal=dead_signal, category_read=category_read, entrypoint_error_category=category)
        return result

    def _record_auth_readiness_failure(
        self, record: SessionRecord, *, identity: _AuthPaneIdentity | None,
        start_time: int | None, pane_observed: bool, reason: str,
        exact_recovery: bool, timeout: float, elapsed: float,
        recovery_observed: bool | None, conversation_matches: bool | None,
        cwd_matches: bool | None,
    ) -> None:
        """Capture only after the original deadline has already refused readiness."""
        processes = []
        if identity is not None and start_time is not None:
            root = self._auth_readiness_process_diagnostic(record, identity.pane_pid, start_time)
            processes.append(root)
            if root["process_state"] in {"live", "stopped"}:
                for pid in process_tree(identity.pane_pid):
                    if pid == identity.pane_pid:
                        continue
                    if len(processes) >= 8:
                        break
                    child = self._auth_process_identity(pid)
                    if child is not None:
                        processes.append(self._auth_readiness_process_diagnostic(record, pid, child[1]))
        value = {"schema": "t1000.auth-readiness-failure.v3",
            "phase": "exact-recovery" if exact_recovery else "launcher-scope",
            "last_failed_gate": reason, "deadline_reached": True,
            "timeout_seconds": max(0.05, timeout), "elapsed_seconds": round(elapsed, 3),
            "pane_observed_at_deadline": pane_observed,
            "recovery_observed": recovery_observed,
            "conversation_matches": conversation_matches, "cwd_matches": cwd_matches,
            "processes": processes,
            "candidate_exit": self._auth_readiness_exit_diagnostic(record, identity, start_time)}
        failures = getattr(self, "_auth_readiness_failures", {})
        if record.id not in failures and len(failures) >= 128:
            failures.pop(next(iter(failures)))
        failures[record.id] = (failures.get(record.id, []) + [value])[-4:]
        self._auth_readiness_failures = failures

    def _await_auth_recreation_ready(
        self,
        record: SessionRecord,
        *,
        previous_session_created: str | None,
        previous_pane_id: str | None,
        previous_pane_pid: int | None,
        previous_pane_start_time: int | None,
        expected_recovery_command: str | None,
        expected_recovery_cwd: str | None,
        timeout: float = 3.0,
    ) -> tuple[_AuthPaneIdentity | None, str]:
        """Wait boundedly for a fresh, process-bound, exact recovery receipt.

        tmux exposes ``session_created`` at one-second resolution.  A valid
        destroy/recreate transaction can therefore share that timestamp with
        the old session. Freshness is rejected only when the complete prior
        session/pane/process identity survives, including the kernel start time.
        """

        deadline = time.monotonic() + max(0.05, timeout)
        last_detail = "fresh tmux pane was not ready"
        last_identity = None
        last_start = None
        recovery_observed = conversation_matches = cwd_matches = None
        while True:
            recovery_observed = conversation_matches = cwd_matches = None
            try:
                identity = self._inspect_auth_pane(record)
            except (OSError, TmuxError) as exc:
                identity = None
                last_detail = f"fresh tmux pane could not be inspected: {exc}"
            if identity is not None:
                process_identity = self._auth_process_identity(identity.pane_pid)
                if process_identity is not None:
                    last_identity, last_start = identity, process_identity[1]
                elif last_identity is None or last_identity.pane_pid != identity.pane_pid:
                    last_identity, last_start = identity, None
                reused_previous_identity = bool(
                    previous_session_created
                    and previous_pane_id
                    and previous_pane_pid is not None
                    and previous_pane_start_time is not None
                    and identity.session_created == previous_session_created
                    and identity.pane_id == previous_pane_id
                    and identity.pane_pid == previous_pane_pid
                    and process_identity is not None
                    and process_identity[1] == previous_pane_start_time
                )
                if reused_previous_identity:
                    last_detail = "tmux reused the old session, pane, and process identity"
                elif process_identity is None:
                    last_detail = "fresh pane process identity was unavailable"
                elif not (
                    self._private_auth_process_tree_environment_ready(
                        record,
                        identity.pane_pid,
                    )
                    or (
                        expected_recovery_command is None
                        and (
                            self._private_auth_process_tree_scope_ready(
                                record,
                                identity.pane_pid,
                            )
                            or self._private_managed_bash_environment_ready(
                                record,
                                identity.pane_pid,
                                process_identity[1],
                            )
                        )
                    )
                ):
                    last_detail = "private auth handoff launcher did not become ready"
                elif expected_recovery_command is None:
                    return identity, ""
                else:
                    try:
                        observed, recovery_command, recovery_cwd = self._auth_recovery_metadata(
                            record, identity.pane_pid
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        last_detail = f"exact Codex recovery could not be observed: {exc}"
                    else:
                        recovery_observed = observed
                        conversation_matches = recovery_command == expected_recovery_command
                        cwd_matches = recovery_cwd == expected_recovery_cwd
                        if (
                            observed
                            and recovery_command == expected_recovery_command
                            and recovery_cwd == expected_recovery_cwd
                        ):
                            return identity, ""
                        last_detail = "fresh pane has not taken over the exact Codex recovery UUID"
            if time.monotonic() >= deadline:
                try:
                    reasons = {
                        "fresh tmux pane was not ready": "pane-unavailable",
                        "tmux reused the old session, pane, and process identity": "old-incarnation",
                        "fresh pane process identity was unavailable": "process-read-ambiguous",
                        "private auth handoff launcher did not become ready": "private-environment",
                        "fresh pane has not taken over the exact Codex recovery UUID": "conversation-or-cwd",
                    }
                    reason = reasons.get(last_detail, "recovery-read-ambiguous" if last_detail.startswith(
                        "exact Codex recovery could not be observed:") else "pane-read-ambiguous")
                    self._record_auth_readiness_failure(record, identity=last_identity,
                        start_time=last_start, pane_observed=identity is not None, reason=reason,
                        exact_recovery=expected_recovery_command is not None, timeout=timeout,
                        elapsed=time.monotonic() - (deadline - max(0.05, timeout)),
                        recovery_observed=recovery_observed,
                        conversation_matches=conversation_matches, cwd_matches=cwd_matches)
                except Exception:
                    # Diagnostic collection has no account authority and cannot replace refusal.
                    pass
                return identity, last_detail
            time.sleep(0.025)

    def apply_auth_change(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
        quiescence: AuthQuiesceResult,
    ) -> AuthActivationResult:
        """Apply one quiescence receipt and always surrender its lifetime lease."""

        try:
            return self._apply_auth_change(record, plan, quiescence)
        finally:
            try:
                self._cancel_confined_resume(record, quiescence.launch_spec)
            finally:
                quiescence.release_lifetime_lease()

    def _apply_auth_change(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
        quiescence: AuthQuiesceResult,
    ) -> AuthActivationResult:
        """Activate the selected account while preserving tmux pane identity."""

        if not plan.ok or not quiescence.ok or quiescence.plan_token != self._auth_plan_token(plan):
            return AuthActivationResult(
                False,
                False,
                detail="auth activation requires a matching successful quiescence receipt",
            )
        lease_required = not plan.live or plan.restart
        if lease_required and (
            not quiescence.lifetime_lease_ready
            or quiescence.lifetime_lease is None
            or not quiescence.lifetime_lease.matches(
                _auth_legacy_lifetime_lock_path(record),
                _auth_lifetime_lock_path(record),
            )
        ):
            return self._blocked_auth_activation(
                plan,
                quiescence,
                "auth activation requires the launcher's matching lifetime-lease receipt",
            )
        if getattr(record, "external", False) or not self._private_auth_scope_configured(record):
            return self._blocked_auth_activation(
                plan,
                quiescence,
                "terminal auth scope changed after quiescence",
            )

        if not plan.live:
            try:
                live = self._auth_session_exists(record)
            except (OSError, TmuxError) as exc:
                return self._blocked_auth_activation(
                    plan,
                    quiescence,
                    f"could not verify the terminal stayed stopped: {exc}",
                )
            if live:
                return self._blocked_auth_activation(
                    plan,
                    quiescence,
                    "terminal started during the account transaction",
                )
            quiescence.release_lifetime_lease()
            return AuthActivationResult(
                True,
                False,
                status="stopped",
                detail="selected account is active for this terminal",
                preserve_selected_account=True,
                credential_lifecycle="stopped",
            )

        if quiescence.gated_respawn:
            replacement = quiescence.replacement_record
            gate_path = Path(quiescence.gate_path) if quiescence.gate_path else None
            if (
                replacement is None
                or quiescence.launch_spec is None
                or gate_path is None
                or quiescence.input_pane_id != plan.pane_id
                or plan.pane_id is None
            ):
                return self._blocked_auth_activation(
                    plan,
                    quiescence,
                    "in-place Codex handoff receipt is incomplete",
                )
            identity: _AuthPaneIdentity | None = None
            readiness_error = ""
            try:
                # Open while still holding the old writer's lifetime lease. The
                # private launcher blocks on that same lease until this method
                # releases it, closing the pointer-change/launch race.
                self._open_auth_switch_gate(gate_path)
                quiescence.release_lifetime_lease()
                identity, readiness_error = self._await_auth_recreation_ready(
                    replacement,
                    previous_session_created=plan.session_created,
                    previous_pane_id=plan.pane_id,
                    previous_pane_pid=plan.pane_pid,
                    previous_pane_start_time=plan.pane_start_time,
                    expected_recovery_command=plan.recovery_command,
                    expected_recovery_cwd=plan.recovery_cwd,
                    timeout=15.0,
                )
            except (OSError, TmuxError) as exc:
                readiness_error = str(exc)
            input_error = ""
            try:
                self._restore_auth_pane_input(plan.pane_id, quiescence.input_was_off)
            except (OSError, TmuxError) as exc:
                input_error = str(exc)
            self._cleanup_auth_switch_gate(gate_path)
            if identity is None or readiness_error or input_error:
                detail = readiness_error or "resumed Codex process was not ready"
                if input_error:
                    detail = f"{detail}; terminal input could not be restored: {input_error}"
                # An observed pane PID is not enough to call the payload live:
                # readiness_error means exact recovery was not proven, while an
                # input error leaves the terminal unusable. Persist the durable
                # recovery recipe, but fail closed on status and PID so startup
                # or an explicit reopen can retry the exact conversation.
                return AuthActivationResult(
                    False,
                    False,
                    recovery_command=plan.recovery_command,
                    recovery_cwd=plan.recovery_cwd,
                    pid=None,
                    status="recovery-pending",
                    detail=detail,
                    preserve_selected_account=True,
                    command=plan.replacement_command,
                    cwd=plan.replacement_cwd,
                    clear_recovery=plan.recovery_command is None,
                )
            pid = identity.pane_pid
            status = "running"
            if record.status.casefold() == "paused":
                paused_pid = self.pause(replacement)
                if paused_pid != pid:
                    return AuthActivationResult(
                        False,
                        False,
                        recovery_command=plan.recovery_command,
                        recovery_cwd=plan.recovery_cwd,
                        pid=pid,
                        status="running",
                        detail="resumed Codex process could not restore the paused state",
                        preserve_selected_account=True,
                        command=plan.replacement_command,
                        cwd=plan.replacement_cwd,
                        clear_recovery=plan.recovery_command is None,
                    )
                status = "paused"
            return AuthActivationResult(
                True,
                True,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                pid=pid,
                status=status,
                detail="selected account is active; exact Codex conversation resumed in place",
                preserve_selected_account=True,
                command=plan.replacement_command,
                cwd=plan.replacement_cwd,
                clear_recovery=plan.recovery_command is None,
                credential_lifecycle="codex-restart" if plan.recovery_command else "idle-shell",
            )

        if plan.restart:
            return self._blocked_auth_activation(
                plan,
                quiescence,
                "restartable Codex handoff did not preserve the tmux pane",
            )
        if quiescence.input_pane_id != plan.pane_id or plan.pane_id is None:
            return self._blocked_auth_activation(
                plan,
                quiescence,
                "live terminal input-fence receipt is incomplete",
            )
        try:
            if not self._identity_matches_plan(self._inspect_auth_pane(record), plan):
                raise TmuxError("live tmux pane changed during account activation")
            if plan.pane_pid is None or process_tree_contains_codex(plan.pane_pid) is not False:
                raise TmuxError("a Codex process appeared during idle-shell account activation")
            if not self._private_auth_process_tree_environment_ready(record, plan.pane_pid):
                raise TmuxError("terminal no longer verifies its private auth environment")
            self._restore_auth_pane_input(plan.pane_id, quiescence.input_was_off)
        except (OSError, TmuxError) as exc:
            return self._blocked_auth_activation(plan, quiescence, str(exc))
        return AuthActivationResult(
            True,
            False,
            pid=plan.pane_pid,
            status=record.status,
            detail="selected account is active; terminal remained ready",
            preserve_selected_account=True,
            credential_lifecycle="idle-shell",
        )

    def rollback_auth_change(
        self,
        record: SessionRecord,
        plan: AuthActivationPlan,
        quiescence: AuthQuiesceResult,
    ) -> AuthActivationResult:
        """Resume the exact payload under a restored previous account pointer."""

        if not quiescence.gated_respawn:
            return self.apply_auth_change(record, plan, quiescence)
        replacement = quiescence.replacement_record
        if replacement is None or plan.pane_id is None:
            return self._blocked_auth_activation(
                plan,
                quiescence,
                "previous account was restored but the handoff receipt is incomplete",
            )
        current = self._inspect_auth_pane(record)
        previous_session_created = current.session_created if current else plan.session_created
        previous_pane_id = current.pane_id if current else plan.pane_id
        previous_pane_pid = current.pane_pid if current else plan.pane_pid
        current_process = (
            self._auth_process_identity(current.pane_pid) if current is not None else None
        )
        previous_start = current_process[1] if current_process is not None else plan.pane_start_time
        old_processes: tuple[tuple[int, int, bool], ...] = ()
        if current is not None:
            try:
                old_processes = self._snapshot_auth_process_tree(current.pane_pid)
            except (OSError, TmuxError):
                old_processes = ()
        gate_path: Path | None = None
        spec: _LaunchSpec | None = None
        try:
            spec = self._launch_spec(replacement)
            gate_path = self._auth_switch_gate_path(record)
            rollback_plan = replace(
                plan,
                session_created=previous_session_created,
                pane_id=previous_pane_id,
                pane_pid=previous_pane_pid,
                pane_start_time=previous_start,
            )
            self._respawn_auth_pane_gated(replacement, rollback_plan, spec, gate_path)
            placeholder, detail = self._await_auth_recreation_ready(
                replacement,
                previous_session_created=previous_session_created,
                previous_pane_id=previous_pane_id,
                previous_pane_pid=previous_pane_pid,
                previous_pane_start_time=previous_start,
                expected_recovery_command=None,
                expected_recovery_cwd=None,
                timeout=4.0,
            )
            if placeholder is None or detail:
                raise TmuxError(detail or "previous-account handoff launcher was not ready")
            if old_processes:
                self._kill_quiesced_auth_processes(old_processes)
            self._open_auth_switch_gate(gate_path)
            identity, detail = self._await_auth_recreation_ready(
                replacement,
                previous_session_created=previous_session_created,
                previous_pane_id=previous_pane_id,
                previous_pane_pid=previous_pane_pid,
                previous_pane_start_time=previous_start,
                expected_recovery_command=plan.recovery_command,
                expected_recovery_cwd=plan.recovery_cwd,
                timeout=15.0,
            )
            if identity is None or detail:
                raise TmuxError(detail or "previous account payload did not resume")
            self._restore_auth_pane_input(plan.pane_id, quiescence.input_was_off)
            return AuthActivationResult(
                True,
                True,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                pid=identity.pane_pid,
                status="running",
                detail="previous account restored; exact Codex conversation resumed in place",
                preserve_selected_account=True,
                command=plan.replacement_command,
                cwd=plan.replacement_cwd,
                clear_recovery=plan.recovery_command is None,
            )
        except (OSError, TmuxError) as exc:
            try:
                self._restore_auth_pane_input(plan.pane_id, quiescence.input_was_off)
            except (OSError, TmuxError):
                pass
            return AuthActivationResult(
                False,
                False,
                recovery_command=plan.recovery_command,
                recovery_cwd=plan.recovery_cwd,
                status="recovery-pending",
                detail=f"previous account restored, but exact Codex recovery failed: {exc}",
                preserve_selected_account=True,
                command=plan.replacement_command,
                cwd=plan.replacement_cwd,
                clear_recovery=plan.recovery_command is None,
            )
        finally:
            self._cleanup_auth_switch_gate(gate_path)
            self._cancel_confined_resume(record, spec)

    def activate_auth_change(self, record: SessionRecord) -> AuthActivationResult:
        """Revalidate, quiesce, and reactivate without changing credentials."""

        if self._private_auth_scope_configured(record) and not _auth_transaction_is_held(record):
            with auth_transaction_lock(record):
                return self.activate_auth_change(record)
        plan = self.plan_auth_change(record)
        quiescence = self.quiesce_auth_change(record, plan)
        try:
            if not quiescence.ok:
                if quiescence.terminated or quiescence.suspended or quiescence.gated_respawn:
                    return self._blocked_auth_activation(
                        plan,
                        quiescence,
                        quiescence.detail,
                    )
                return AuthActivationResult(False, False, detail=quiescence.detail)
            return self.apply_auth_change(record, plan, quiescence)
        finally:
            quiescence.release_lifetime_lease()

    @classmethod
    def _saved_direct_bash_command(cls, record: SessionRecord, command: str) -> bool:
        """One registered system Bash executable, with at most a single -i.

        The saved interactive flag is equivalent to the managed rc launch's
        mandatory -i. No script, startup-file option or other shell flag gains
        this admission, and recovery metadata keeps its existing native gate.
        """

        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        if (
            not argv
            or len(argv) > 2
            or (len(argv) == 2 and argv[1] != "-i")
            or Path(argv[0]).name != "bash"
            or command.strip() != shlex.join(argv)
        ):
            return False
        return cls._recovery_pending_shell_can_start_fresh(record, shlex.quote(argv[0]))

    @classmethod
    def _recovery_pending_shell_can_start_fresh(
        cls,
        record: SessionRecord,
        command: str,
    ) -> bool:
        """Recognize the one recovery-pending state that is safe to recreate.

        A single absolute executable naming a supported interactive shell has no
        hidden conversation identity.  Any recovery command/directory, relative
        lookup, argument, malformed quoting, or non-shell executable makes the
        record ineligible so exact agent/checkpoint recovery remains fail-closed.
        """

        if (getattr(record, "recovery_command", None) or "").strip():
            return False
        if (getattr(record, "recovery_cwd", None) or "").strip():
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        if len(argv) != 1:
            return False
        shell_path = Path(argv[0])
        if not shell_path.is_absolute():
            return False
        if shell_path.name.casefold().removeprefix("-") not in cls._SUPPORTED_IDLE_SHELLS:
            return False
        try:
            resolved = shell_path.resolve(strict=True)
            metadata = resolved.stat()
            system_shell_owner = Path("/bin/sh").resolve(strict=True).stat().st_uid
            configured_shells = {
                Path(line).resolve(strict=True)
                for line in Path("/etc/shells").read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
                if line.strip().startswith("/")
            }
        except OSError:
            return False
        # Recovery may execute persisted data after a crash.  A user-writable
        # impostor merely named ``bash`` is not a trusted system shell even when
        # it is absolute and executable.  Compare ownership with the canonical
        # system shell rather than numeric UID 0 so rootless validation
        # namespaces (where root maps to overflow UID 65534) retain the same
        # security decision as the host.
        return (
            resolved in configured_shells
            and resolved.is_file()
            and metadata.st_uid == system_shell_owner
            and (metadata.st_mode & 0o022) == 0
            and os.access(resolved, os.X_OK)
        )

    @staticmethod
    def _is_codex_launch_command(command: str) -> bool:
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return safe_codex_recovery_command(argv) is not None

    @staticmethod
    def _auth_recovery_metadata(record: SessionRecord, root_pid: int):
        if record.tag == "web-agent":
            from .agent_launch import observe_resume
            try:
                return observe_resume(record, root_pid)
            except (OSError, ValueError) as exc:
                raise TmuxError(f"Configured task authorization requires review: {exc}") from exc
        return observe_recovery_metadata(root_pid)

    @staticmethod
    def _exact_auth_recovery(record: SessionRecord) -> str | None:
        if record.tag == "web-agent":
            from .agent_launch import resume_identity
            return record.recovery_command if resume_identity(record.recovery_command) else None
        return TmuxBackend._exact_codex_recovery(record)

    @staticmethod
    def _exact_codex_recovery(record: SessionRecord) -> str | None:
        raw = (getattr(record, "recovery_command", None) or "").strip()
        if not raw:
            return None
        try:
            command = safe_codex_recovery_command(shlex.split(raw))
        except ValueError:
            return None
        if command is None:
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        return command if "resume" in parts else None

    @staticmethod
    def _exact_claude_recovery(record: SessionRecord) -> str | None:
        raw = (getattr(record, "recovery_command", None) or "").strip()
        if not raw:
            return None
        try:
            return safe_claude_recovery_command(shlex.split(raw))
        except ValueError:
            return None

    def checkpoint_records(
        self,
        records: Sequence[SessionRecord],
    ) -> list[CheckpointResult]:
        """Persist exact restart state for every selected live managed tab."""

        rows = list(records)
        live_by_id = self.session_exists_many(rows)
        results: list[CheckpointResult] = []
        for record in rows:
            if getattr(record, "external", False):
                results.append(
                    CheckpointResult(
                        record.id,
                        "checkpoint",
                        False,
                        False,
                        "external tmux sessions cannot be checkpointed",
                    )
                )
                continue
            if not live_by_id.get(record.id, False):
                results.append(
                    CheckpointResult(
                        record.id,
                        "checkpoint",
                        False,
                        False,
                        "managed tmux session is not running",
                    )
                )
                continue
            exact_codex = self._exact_codex_recovery(record)
            if exact_codex:
                results.append(
                    CheckpointResult(
                        record.id,
                        "codex-resume",
                        True,
                        True,
                        "exact Codex session UUID persisted",
                    )
                )
                continue
            results.append(self.checkpoints.checkpoint(record))
        return results

    def send_text(self, record: SessionRecord, text: str, enter: bool = True) -> None:
        self.create_or_resume(record)
        # Exit copy-mode / Codex transcript view before sending so keys reach
        # the live composer rather than feeding the history cursor. This must
        # run even when tmux entered copy mode outside TermDeck, in which case
        # there is deliberately no entry in ``_scroll_views``.
        if not self.restore_live_session(record, force=True):
            raise TmuxError(
                "terminal could not be restored from history before sending input"
            )
        if text:
            self._run(["send-keys", "-t", self.pane_target(record), "-l", text])
        if enter:
            self._run(["send-keys", "-t", self.pane_target(record), "C-m"])

    def capture(self, record: SessionRecord, lines: int = 200) -> str:
        clean_lines = min(self.max_capture_lines, max(1, int(lines)))
        start = f"-{clean_lines}"
        # capture-pane already reports a missing target. Avoid a redundant
        # has-session subprocess, especially for fleet-wide activity queries.
        result = self._run(
            ["capture-pane", "-t", self.pane_target(record), "-p", "-S", start],
            check=False,
            timeout=self._query_timeout_for(clean_lines),
        )
        return result.stdout if result.returncode == 0 else ""

    def pause(self, record: SessionRecord) -> int | None:
        pid = self.pane_pid(record)
        if not pid:
            return None
        self._signal_process_group(pid, signal.SIGSTOP)
        return pid

    def resume(self, record: SessionRecord) -> int | None:
        pid = self.pane_pid(record)
        if not pid:
            return None
        self._signal_process_group(pid, signal.SIGCONT)
        return pid

    def terminate(self, record: SessionRecord) -> bool:
        """Stop one exact session, preserving recovery state until exit is proven.

        Return whether the kill command succeeded. False means a fresh query
        confirmed that the session had already ended, including concurrent exit.
        An unavailable control plane is never evidence that cleanup is safe.
        """

        result = self._run(["kill-session", "-t", f"={record.tmux_name}"], check=False)
        stopped = result.returncode == 0
        if not stopped:
            # A session can exit while the stop request is in flight. Use an
            # exact-name inventory after failure; session_exists deliberately
            # permits stale UI observations and cannot authorize destruction of
            # durable recovery state. Never retry the kill against a new target.
            observed = self._run(
                ["list-sessions", "-F", "#{session_name}"],
                check=False,
                timeout=self.command_timeout,
                bypass_query_backoff=True,
            )
            if observed.returncode == 0:
                names = set(observed.stdout.splitlines())
                if record.tmux_name in names:
                    detail = result.stderr.strip() or "tmux kill-session failed"
                    raise TmuxError(f"{detail}; terminal session is still present")
            elif not self._server_is_absent(observed):
                detail = observed.stderr.strip() or "tmux session inventory is unavailable"
                raise TmuxError(f"could not verify terminal stop: {detail}")

        with self._history_session_lock_for(record.id):
            self._history_pop(record.id)
        # A deliberate Stop/Kill must not resurrect stale process memory on the
        # next TermDeck launch. Browser-style Hide leaves the checkpoint intact.
        self.checkpoints.clear(record)
        self.invalidate_cache()
        return stopped

    def rename(self, record: SessionRecord, new_name: str) -> None:
        if self.session_exists(record):
            self._run(["rename-session", "-t", record.tmux_name, new_name], check=False)
        self.invalidate_cache()

    @staticmethod
    def _signal_process_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise TmuxError(f"permission denied while signalling process group for pid {pid}") from exc
        except OSError:
            # Some shells/programs may not be in a process group accessible this
            # way. Fall back to the direct pane PID.
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return


    def list_session_names(self) -> list[str]:
        """Return live tmux session names for diagnostics and adoption previews."""

        result = self._run(["list-sessions", "-F", "#{session_name}"], check=False)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux list-sessions failed")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_panes_text(self, format_string: str) -> str:
        result = self._run(["list-panes", "-a", "-F", format_string], check=False)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux list-panes failed")
        return result.stdout

    def debug_command(self, args: Sequence[str]) -> str:
        return " ".join(shlex.quote(part) for part in [self.executable, *args])


def _declared_auth_lifecycle(backend):
    """Return the safe lifecycle only when methods are genuinely declared."""

    names = ("plan_auth_change", "quiesce_auth_change", "apply_auth_change")
    declared = []
    for name in names:
        method = inspect.getattr_static(backend, name, None)
        if not callable(method):
            return None
        declared.append(getattr(backend, name))
    return tuple(declared)


def execute_auth_change(
    backend,
    manager,
    record: SessionRecord,
    previous_state: CodexAuthState,
    account_query: str,
) -> AuthTransactionResult:
    """Run one atomic, recoverable account transaction for GTK and the CLI."""

    if (
        isinstance(backend, TmuxBackend)
        and backend._private_auth_scope_configured(record)
        and not _auth_transaction_is_held(record)
    ):
        return AuthTransactionResult(
            False,
            error="Codex auth transaction requires the private scope lock",
        )
    try:
        account = resolve_account(previous_state, account_query)
    except CodexAuthError as exc:
        return AuthTransactionResult(False, error=str(exc))

    lifecycle = _declared_auth_lifecycle(backend)
    if lifecycle is None:
        return AuthTransactionResult(
            False,
            error="terminal backend lacks the safe Codex auth handoff lifecycle",
        )
    planner, quiescer, applier = lifecycle
    try:
        plan = planner(record)
    except Exception as exc:
        return AuthTransactionResult(False, error=f"Codex auth preflight failed: {exc}")
    if not bool(getattr(plan, "ok", False)):
        return AuthTransactionResult(
            False,
            error=str(getattr(plan, "detail", "") or "Codex auth preflight failed"),
        )
    try:
        quiescence = quiescer(record, plan)
    except Exception as exc:
        return AuthTransactionResult(False, error=f"Codex auth handoff failed: {exc}")
    if not bool(getattr(quiescence, "ok", False)):
        if any(
            bool(getattr(quiescence, name, False))
            for name in ("terminated", "suspended", "gated_respawn")
        ):
            activation = TmuxBackend._blocked_auth_activation(
                plan,
                quiescence,
                str(
                    getattr(quiescence, "detail", "")
                    or "terminal could not be restored after auth handoff failed"
                ),
            )
            return AuthTransactionResult(
                False,
                switch_result=CodexSwitchResult(True, previous_state),
                activation=activation,
                error=activation.detail,
            )
        return AuthTransactionResult(
            False,
            error=str(
                getattr(quiescence, "detail", "")
                or "terminal could not be prepared for account switching"
            ),
        )

    def _restore_previous_binding() -> tuple[
        CodexSwitchResult | None,
        str,
        str,
    ]:
        try:
            restored = manager.restore(record, previous_state.selected_account_key)
        except Exception as exc:
            restored = None
            restore_error = str(exc) or exc.__class__.__name__
        else:
            if restored.ok and (
                restored.state.selected_account_key != previous_state.selected_account_key
                or not restored.state.canonical_binding_ready
            ):
                restore_error = "previous canonical Codex account binding was not verified"
            else:
                restore_error = restored.error if not restored.ok else ""
        if restored is not None and restore_error:
            restored = CodexSwitchResult(False, restored.state, restore_error)
        observation_error = ""
        if restored is None or not restored.ok:
            try:
                observed = manager.state(record)
            except Exception as exc:
                observed = replace(
                    previous_state,
                    selected_account=None,
                    selected_account_key=None,
                    fingerprint=f"unverified:{previous_state.fingerprint}",
                    canonical_binding_ready=False,
                )
                observation_error = str(exc) or exc.__class__.__name__
            if restored is None or restored.state != observed:
                restored = CodexSwitchResult(
                    False,
                    observed,
                    restore_error or "previous Codex account restore was not verified",
                )
        return restored, restore_error, observation_error

    try:
        lease_required = isinstance(backend, TmuxBackend) and (
            not plan.live or plan.restart
        )
        if lease_required:
            lease = getattr(quiescence, "lifetime_lease", None)
            if (
                not isinstance(lease, _AuthLifetimeLease)
                and not callable(getattr(lease, "matches", None))
            ) or not bool(
                lease.matches(
                    _auth_legacy_lifetime_lock_path(record),
                    _auth_lifetime_lock_path(record),
                )
            ):
                activation = TmuxBackend._blocked_auth_activation(
                    plan,
                    quiescence,
                    "Codex auth lifetime lease changed before account selection",
                )
                return AuthTransactionResult(
                    False,
                    switch_result=CodexSwitchResult(True, previous_state),
                    activation=activation,
                    error=activation.detail,
                )

        switch_error = ""
        switched: CodexSwitchResult | None = None
        try:
            switched = manager.switch(record, account.account_key)
            if not switched.ok:
                switch_error = switched.error or "private Codex account switch failed"
            elif (
                switched.state.selected_account_key != account.account_key
                or not switched.state.canonical_binding_ready
            ):
                switch_error = (
                    "private Codex account switch did not verify the requested "
                    "canonical binding"
                )
        except Exception as exc:
            switch_error = str(exc) or exc.__class__.__name__

        if not switch_error and switched is not None and switched.ok:
            try:
                activation = applier(record, plan, quiescence)
            except Exception as exc:
                activation = TmuxBackend._blocked_auth_activation(
                    plan,
                    quiescence,
                    f"terminal activation failed after account selection: {exc}",
                )
            if activation.ok:
                # The concrete credential manager records the new process
                # incarnation only after the guarded lifecycle proves readiness.
                recorder = inspect.getattr_static(manager, "record_activation", None)
                if callable(recorder):
                    try:
                        manager.record_activation(record, switched.state, activation)
                    except (OSError, ValueError, RuntimeError):
                        return AuthTransactionResult(
                            False, switch_result=switched, activation=replace(activation, ok=False),
                            error="Account selection was applied, but its activation receipt could not be saved; inspect before retrying",
                        )
                return AuthTransactionResult(
                    True,
                    switch_result=switched,
                    activation=activation,
                )

            # A process may have started with the requested token before a late
            # readiness check failed. Restore the old canonical pointer and
            # force one same-pane exact recovery under it.
            restored, restore_error, observation_error = _restore_previous_binding()
            if restored is None or not restored.ok:
                detail = activation.detail or "selected account could not be activated"
                detail = (
                    f"{detail}; previous account restore failed: "
                    f"{restore_error or 'not verified'}"
                )
                if observation_error:
                    detail += f"; current private account could not be inspected: {observation_error}"
                return AuthTransactionResult(
                    False,
                    switch_result=restored or switched,
                    activation=activation,
                    error=detail,
                )
            rollback_fn = getattr(backend, "rollback_auth_change", None)
            try:
                rollback = (
                    rollback_fn(record, plan, quiescence)
                    if callable(rollback_fn)
                    else applier(record, plan, quiescence)
                )
            except Exception as exc:
                rollback = TmuxBackend._blocked_auth_activation(
                    plan,
                    quiescence,
                    f"previous account was restored but terminal recovery failed: {exc}",
                )
            detail = activation.detail or "selected account could not be activated"
            if not rollback.ok:
                detail += (
                    "; previous account restored, but exact terminal recovery failed: "
                    f"{rollback.detail}"
                )
            return AuthTransactionResult(
                False,
                switch_result=restored,
                activation=rollback,
                error=detail,
                rolled_back=True,
            )

        # No requested pointer was accepted. Restore and verify the exact old
        # binding while the gated launcher is still blocked, then open it.
        restored, restore_error, observation_error = _restore_previous_binding()
        if restored is None or not restored.ok:
            activation = TmuxBackend._blocked_auth_activation(
                plan,
                quiescence,
                "previous Codex account could not be verified; terminal remains fenced",
            )
            detail = switch_error or "private Codex account switch failed"
            detail += (
                f"; previous account restore failed: {restore_error or 'not verified'}"
            )
            if observation_error:
                detail += f"; current private account could not be inspected: {observation_error}"
            return AuthTransactionResult(
                False,
                switch_result=restored,
                activation=activation,
                error=detail,
            )

        try:
            activation = applier(record, plan, quiescence)
        except Exception as exc:
            activation = TmuxBackend._blocked_auth_activation(
                plan,
                quiescence,
                f"previous account was restored but the terminal could not resume: {exc}",
            )
        detail = switch_error or "private Codex account switch failed"
        if not activation.ok:
            detail += (
                "; previous account restored, but terminal recovery failed: "
                f"{activation.detail}"
            )
        return AuthTransactionResult(
            False,
            switch_result=restored,
            activation=activation,
            error=detail,
            rolled_back=True,
        )
    finally:
        try:
            if isinstance(backend, TmuxBackend):
                backend._cancel_confined_resume(record, quiescence.launch_spec)
        finally:
            release = getattr(quiescence, "release_lifetime_lease", None)
            if callable(release):
                release()
