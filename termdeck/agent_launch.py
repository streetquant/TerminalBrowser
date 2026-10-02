"""Authenticated host-user terminals with immutable attachment launch receipts."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

from .client_files import Actor, Directory, StagingError, _database, _id, _json, _text, authorized
from .models import SessionRecord
from .session_store import SessionStore, utc_now
from . import server_workspaces


POLICY = "web-launch-policy.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS launches(launch_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,machine_id TEXT NOT NULL,
 session_id TEXT NOT NULL,request_id TEXT NOT NULL,body_digest TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,
 UNIQUE(account_id,machine_id,session_id,request_id));
"""
RUNS = "CREATE TABLE IF NOT EXISTS launch_runs(launch_id TEXT NOT NULL,tab_id TEXT NOT NULL,PRIMARY KEY(launch_id,tab_id));"
RESUMES = """CREATE TABLE IF NOT EXISTS launch_resumes(
 permit_id TEXT PRIMARY KEY,launch_id TEXT NOT NULL,tab_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL,codex_home TEXT NOT NULL,scope_id TEXT NOT NULL,
 used INTEGER NOT NULL DEFAULT 0);"""
_RESUME_PREFIX = "termdeck-confined-resume:"
_MASKED_NAMES = (".termdeck", ".beads")
_PLAN_VERSION = 2
_EXECUTION = "host-user"
_POLICY_CONTEXT = b"termdeck-host-user-launch-v2\x00"
_LEGACY_PLAN_ERROR = "Legacy confined launch requires a new authenticated host-user launch"


def _within(path, root):
    return path == root or root in path.parents


def _runtime_roots(state_root):
    state_root = Path(state_root)
    return (state_root, *(p for p in state_root.parents if p.name == ".termdeck"))


def _require_host_plan(plan):
    if (type(plan.get("version")) is not int or plan["version"] != _PLAN_VERSION
            or plan.get("execution") != _EXECUTION):
        raise StagingError(_LEGACY_PLAN_ERROR, "conflict")


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise StagingError("Duplicate fields in saved launch state", "unsafe-storage")
        value[key] = item
    return value


def _load_object(raw, label):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except StagingError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StagingError(f"Invalid saved {label}", "unsafe-storage") from exc
    if not isinstance(value, dict):
        raise StagingError(f"Invalid saved {label}", "unsafe-storage")
    return value


def _actor_from_plan(value):
    fields = {"account_id", "machine_id", "session_id", "credential_hash", "credential_generation"}
    if not isinstance(value, dict) or set(value) != fields:
        raise StagingError("Invalid launch actor", "unauthorized")
    actor = Actor(account_id=value["account_id"], machine_id=value["machine_id"],
                  session_id=value["session_id"], credential_hash=value["credential_hash"],
                  credential_generation=value["credential_generation"])
    actor.validate()
    return actor


@contextmanager
def _plan_workspace(store, plan, value, *, inspect=False):
    _require_host_plan(plan)
    with server_workspaces.open_workspace(plan["workspace"]) as directory:
        yield directory


def _shared_asset_path(path, *, state_root, auth_directory, account_source,
                       private_home, workspace, shared_names):
    """Check the resolved grant, not merely the shared asset's link name."""
    target = Path(path).resolve(strict=True)
    target = _host_path(str(target), directory=target.is_dir())
    _validate_shared_location(target, state_root=state_root, auth_directory=auth_directory,
                              account_source=account_source, private_home=private_home,
                              workspace=workspace, shared_names=shared_names)
    return str(target)


def _validate_shared_location(target, *, state_root, auth_directory, account_source,
                              private_home, workspace, shared_names):
    source = Path(account_source)
    private = (Path(auth_directory), Path(state_root) / "terminal-auth", Path(private_home))
    runtime = (*_runtime_roots(state_root), *(Path(workspace) / n for n in _MASKED_NAMES))
    # No shared asset may grant a protected tree or any of its ancestors.
    if any(_within(root, target) for root in (source, *private, *runtime)):
        raise StagingError("Shared asset would expose protected storage", "unsafe-storage")
    if any(_within(target, root) for root in private):
        raise StagingError("Shared asset would expose private authentication", "unsafe-storage")
    source_asset = False
    if _within(target, source):
        relative = target.relative_to(source)
        source_asset = relative.parts[0] in shared_names
        if not source_asset or any(name in relative.parts for name in _MASKED_NAMES):
            raise StagingError("Shared asset is outside the non-auth catalogue assets", "unsafe-storage")
    # A catalogue inside managed state may expose its explicit non-auth assets,
    # but aliases into other runtime/ledger data are never implicit grants.
    if any(_within(target, root) for root in runtime) and not source_asset:
        raise StagingError("Shared asset would expose runtime storage", "unsafe-storage")
    if ".beads" in target.parts or ".termdeck" in target.parts and not source_asset:
        raise StagingError("Shared asset would expose protected storage", "unsafe-storage")


def _validate_shared_assets(home, *, state_root, auth_directory, account_source, workspace):
    """Validate automatic configuration bindings, independently of host access."""
    from .codex_config_scope import _canonical_assets, CodexConfigScopeError
    try:
        assets = [asset for asset in _canonical_assets(Path(account_source)) if asset[0] != ".env"]
        checks = dict(state_root=state_root, auth_directory=auth_directory,
                      account_source=account_source, private_home=home, workspace=workspace,
                      shared_names={name for name, _path, _directory in assets})
        for name, canonical, _directory in assets:
            private = Path(home) / name
            if not private.is_symlink():
                if name != "config.toml" or not private.exists():
                    continue
                from .native_config_view import is_managed_private_config
                if not is_managed_private_config(Path(home), Path(account_source)):
                    raise StagingError("Private native configuration has no validated provenance", "unsafe-storage")
                # A mutable private view never removes validation of the
                # canonical defaults target, including a changed source alias.
                target = Path(_shared_asset_path(canonical, **checks))
                destinations = (target, canonical)
            else:
                target = Path(_shared_asset_path(private, **checks))
                if target != canonical.resolve(strict=True):
                    raise StagingError("Shared asset binding changed", "unsafe-storage")
                # Existing valid links can point directly at a target or use a
                # different alias. Do not rewrite any host or private symlink.
                alias = Path(os.path.abspath(private.parent / os.readlink(private)))
                destinations = (target, canonical, alias)
            for destination in destinations:
                _validate_shared_location(destination, **checks)
    except (OSError, RuntimeError, CodexConfigScopeError) as exc:
        raise StagingError("Shared asset catalogue cannot be validated", "unsafe-storage") from exc


def _prepare_native_configuration(home, *, state_root, auth_directory, account_source, workspace, write=True):
    """Grant-check source bytes before private initialization or read-only planning."""
    from .codex_config_scope import _canonical_assets, CodexConfigScopeError
    from .native_config_view import prepare_private_config
    try:
        source = Path(account_source)
        assets = [asset for asset in _canonical_assets(source) if asset[0] != ".env"]
        checks = dict(state_root=state_root, auth_directory=auth_directory,
                      account_source=account_source, private_home=home, workspace=workspace,
                      shared_names={name for name, _path, _directory in assets})
        canonical = next((path for name, path, _directory in assets if name == "config.toml"), None)
        target = None
        if canonical is not None:
            target = _shared_asset_path(canonical, **checks)
            _validate_shared_location(canonical, **checks)
        return prepare_private_config(Path(home), source, target, write=write)
    except CodexConfigScopeError as exc:
        # The helper's diagnostics contain no configuration values or tokens.
        raise StagingError(str(exc), "configuration-conflict") from None
    except (OSError, RuntimeError):
        raise StagingError("Private native configuration authority cannot be validated", "configuration-conflict") from None


def _prepare_private_log_directory(home):
    """Create only a direct, owned private directory; refuse existing aliases."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = log = None
    try:
        # ubs:ignore[py.io.open-missing-with] -- directory FDs are validated with fstat and closed in the enclosing finally; fdopen cannot wrap O_DIRECTORY descriptors.
        root = os.open(home, flags)
        info = os.fstat(root)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StagingError("Private Codex home has unsafe authority", "unsafe-storage")
        try:
            os.mkdir("log", 0o700, dir_fd=root)
        except FileExistsError:
            pass
        # ubs:ignore[py.io.open-missing-with] -- directory FDs are validated with fstat and closed in the enclosing finally; fdopen cannot wrap O_DIRECTORY descriptors.
        log = os.open("log", flags, dir_fd=root)
        info = os.fstat(log)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StagingError("Private Codex log directory has unsafe authority", "unsafe-storage")
    except OSError as exc:
        raise StagingError("Private Codex log directory is unavailable", "unsafe-storage") from exc
    finally:
        if log is not None:
            os.close(log)
        if root is not None:
            os.close(root)


def _host_path(value, *, directory=False):
    if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
        raise StagingError("Invalid approved host path", "unsafe-storage")
    path=Path(value)
    for parent in (*reversed(path.parents), path):
        info=parent.lstat()
        if stat.S_ISLNK(info.st_mode) or (info.st_mode & 0o022 and parent != Path("/tmp")):
            raise StagingError("Unsafe approved host path", "unsafe-storage")
    info=path.stat()
    if info.st_uid not in {0,os.geteuid()} or (not path.is_dir() if directory else not stat.S_ISREG(info.st_mode)):
        raise StagingError("Invalid approved host object", "unsafe-storage")
    return path


def policy(state_root, *, _auto_refresh=True):
    refresh = None
    with closing(Directory(state_root)) as tree:
        try:
            with tree.file(POLICY) as (fd, _):
                raw = os.read(fd, 65537)
        except FileNotFoundError:
            return {"version": 1, "workspaces": [], "codex": None}, hashlib.sha256(_POLICY_CONTEXT).hexdigest()
        if len(raw) > 65536:
            raise StagingError("Launch policy is too large", "unsafe-storage")
        value = _load_object(raw, "launch policy")
        if (set(value) != {"version", "workspaces", "codex"}
                or isinstance(value["version"], bool) or not isinstance(value["version"], int)
                or value["version"] != 1):
            raise StagingError("Invalid launch policy", "unsafe-storage")
        if not isinstance(value["workspaces"], list) or len(value["workspaces"]) > 64:
            raise StagingError("Invalid workspace policy", "unsafe-storage")
        seen = set()
        for workspace in value["workspaces"]:
            if not isinstance(workspace, dict) or set(workspace) != {"id", "label", "path"}:
                raise StagingError("Invalid workspace policy", "unsafe-storage")
            _id(workspace["id"], "ws_")
            _text(workspace["label"], 256)
            directory = server_workspaces.WorkspaceDirectory(workspace["path"], state_root=state_root, inspect=False)
            try:
                workspace["path"] = str(directory.path)
            finally:
                directory.close()
            if workspace["id"] in seen:
                raise StagingError("Duplicate workspace identity", "unsafe-storage")
            seen.add(workspace["id"])
        codex = value["codex"]
        if codex is not None:
            if not isinstance(codex, dict) or set(codex) != {"executable", "sha256", "account_source"}:
                raise StagingError("Invalid TermDeck Codex runtime policy", "unsafe-storage")
            _id(codex["sha256"], "", 64)
            from .launch_policy import current_npm_runtime, is_npm_native_path, native_digest
            if is_npm_native_path(codex["executable"]):
                try:
                    runtime = current_npm_runtime(codex["executable"])
                    executable = Path(runtime["native"])
                    actual_sha256 = native_digest(executable)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                        subprocess.TimeoutExpired):
                    raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage") from None
                if str(executable) != codex["executable"] or actual_sha256 != codex["sha256"]:
                    if not _auto_refresh:
                        raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage")
                    refresh = (raw, executable, Path(runtime["entrypoint"]))
            else:
                # Preserve pre-existing exact-digest policy behavior for deliberately
                # configured non-npm runtimes; npm installations always take the
                # official current-entrypoint path above and cannot fall back.
                executable = _host_path(codex["executable"])
                if not os.access(executable, os.X_OK):
                    raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage")
                actual_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
                if actual_sha256 != codex["sha256"]:
                    raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage")
            _host_path(codex["account_source"], directory=True)
        revision = hashlib.sha256(_POLICY_CONTEXT + raw).hexdigest()
    if refresh is not None:
        try:
            from .launch_policy import refresh_native_policy
            refresh_native_policy(state_root, refresh[0], refresh[1], refresh[2])
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, StagingError,
                subprocess.TimeoutExpired):
            raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage") from None
        return policy(state_root, _auto_refresh=False)
    return value, revision


def options(state_root, directory=None):
    value,digest=policy(state_root)
    result = {"ok":True,"policy_revision":digest,"workspaces":value["workspaces"],"server_directory":True,
              "runtimes":[{"id":"shell","label":"Native shell"}]+([{"id":"codex","label":"Codex (TermDeck private account)"}] if value["codex"] else [])}
    if directory is not None:
        from .server_workspaces import immediate_directories
        result["directory"] = server_workspaces.select(directory)["path"]
        result["directories"] = immediate_directories(result["directory"])
    return result


def resume_identity(command):
    """An internal identity, never executable shell syntax."""
    if not isinstance(command, str) or not command.startswith(_RESUME_PREFIX):
        return None
    value = command[len(_RESUME_PREFIX):]
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _launch_binding(record):
    parts = shlex.split(record.command)
    if (record.tag != "web-agent" or record.external or len(parts) not in {13, 14}
            or parts[0] != "exec" or not Path(parts[1]).is_absolute()):
        raise StagingError("Configured launch binding is unavailable", "unauthorized")
    if len(parts) == 14 and parts[2:4] == ["-m", "termdeck.agent_launch"]:
        options = parts[4:]
    elif len(parts) == 13 and parts[2] == "agent-launch":
        options = parts[3:]
    else:
        raise StagingError("Configured launch binding is unavailable", "unauthorized")
    if options[::2] != ["--state", "--auth-dir", "--db", "--launch", "--tab"]:
        raise StagingError("Configured launch binding is unavailable", "unauthorized")
    return SimpleNamespace(**dict(zip(("state", "auth_dir", "db", "launch", "tab"), options[1::2])))


def _entrypoint_path(value, *, directory=False):
    """Managed dispatch authority has stricter ownership than a selected cwd."""
    path = _host_path(value, directory=directory)
    for parent in (*path.parents, path):
        if parent.lstat().st_uid not in {0, os.geteuid()}:
            raise StagingError("Managed launch authority has unsafe ownership", "unsafe-storage")
    if not directory and path.lstat().st_nlink != 1:
        raise StagingError("Managed launch authority has multiple links", "unsafe-storage")
    return path


def _managed_entrypoint():
    """Use the current admitted bootstrap without pinning a particular release."""
    names = ("T1000_MANAGED_ROOT", "T1000_RELEASE_STATE", "T1000_RELEASE_MANIFEST")
    if not any(name in os.environ for name in names):
        return None
    from .build_info import release_status
    from .release_manager import load_managed_state, release_layout
    try:
        root = _entrypoint_path(os.environ.get("T1000_MANAGED_ROOT", ""), directory=True)
        state_path = _entrypoint_path(str(root / "state.json"))
        state = load_managed_state(release_layout(root=root))
        if not state["current_release_id"]:
            raise ValueError("No managed current release")
        manifest = _entrypoint_path(str(root / "releases" / state["current_release_id"] / "manifest.json"))
        _entrypoint_path(str(root / "bootstrap.py"))
        launcher = _entrypoint_path(state["launcher_path"])
        if not os.access(launcher, os.X_OK):
            raise ValueError("Managed launcher is not executable")
        if (os.environ.get("T1000_RELEASE_STATE", str(state_path)) != str(state_path)
                or os.environ.get("T1000_RELEASE_MANIFEST", str(manifest)) != str(manifest)):
            raise ValueError("Managed context does not match state")
        status = release_status(managed_root=root)
        if (not status["ok"] or status["posture"] != "managed-release"
                or status["manifest_path"] != str(manifest)
                or state != load_managed_state(release_layout(root=root))):
            raise ValueError("Managed launch identity changed")
        return {"root": str(root), "launcher": str(launcher)}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        raise StagingError("Managed launch authority could not be verified", "unsafe-storage") from None


def _verify_launcher_identity(plan):
    if (plan.get("launcher") != str(Path(sys.executable).resolve(strict=True))
            or plan.get("managed_entrypoint") != _managed_entrypoint()):
        raise StagingError("Configured launcher identity changed", "unauthorized")


def _runtime_argv(value, plan, home, conversation=None):
    # A shared daemon retains its startup account and state paths. Both new
    # tasks and exact resumes must load this tab's private home themselves.
    argv = [value["codex"]["executable"], "-c", f"sqlite_home={json.dumps(home)}",
            "-c", f"log_dir={json.dumps(str(Path(home) / 'log'))}",
            "--no-daemon", "--no-alt-screen"]
    if plan.get("yolo") is True:
        argv.append("--yolo")
    if conversation:
        return [*argv, "resume", conversation]
    prompt = plan["task"] + "\nAuthorized immutable files staged on the TermDeck host:\n" + "\n".join(a["server_path"] for a in plan["attachments"])
    return [*argv, "--", prompt]


@contextmanager
def _pinned_executable(runtime, expected_sha256=None):
    if (not isinstance(runtime, list) or not runtime or not isinstance(runtime[0], str)
            or not runtime[0].startswith("/")):
        raise StagingError("Native executable binding is invalid", "unsafe-storage")
    path = _host_path(runtime[0])
    before = path.stat(follow_symlinks=False)
    if (before.st_uid not in {0, os.geteuid()} or before.st_mode & 0o022
            or not before.st_mode & 0o111 or not os.access(path, os.X_OK)):
        raise StagingError("Native executable authority is unsafe", "unsafe-storage")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK),
                    "rb", buffering=0) as handle:
        descriptor = handle.fileno()
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode,
                    before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid, opened.st_mode,
                           opened.st_nlink, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if identity != opened_identity:
            raise StagingError("Native executable changed while opening", "unsafe-storage")
        header = os.read(descriptor, 20)
        is_native_elf = (header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00")
        is_shebang = header.startswith(b"#!")
        if not is_native_elf and not is_shebang:
            raise StagingError("Executable is neither a supported ELF nor a shebang script", "unsafe-storage")
        if expected_sha256 is not None:
            digest = hashlib.sha256()
            size = 0
            os.lseek(descriptor, 0, os.SEEK_SET)
            while chunk := os.read(descriptor, 1024 * 1024):  # ubs:ignore[py.perf.io-in-loop] -- Streaming keeps the 512-MiB executable hash bound to one-MiB memory chunks.
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise StagingError("Native executable exceeds the verified size limit", "unsafe-storage")
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise StagingError("Qualified Codex executable changed before launch", "unsafe-storage")
        after = path.stat(follow_symlinks=False)
        after_identity = (after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_mode,
                          after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if after_identity != identity:
            raise StagingError("Native executable changed during verification", "unsafe-storage")
        if is_shebang:
            # Linux must pass the script FD through to its interpreter. The
            # descriptor is read-only and names only this already-verified file.
            os.set_inheritable(descriptor, True)
        yield descriptor, [str(path), *runtime[1:]]


def _launcher_command(store, plan, tab_id):
    managed = plan.get("managed_entrypoint")
    entrypoint = ([managed["launcher"], "agent-launch"] if managed is not None
                  else [plan["launcher"], "-m", "termdeck.agent_launch"])
    return "exec " + shlex.join([*entrypoint,
        "--state", str(store.root.parent), "--auth-dir", str(store.auth_directory),
        "--db", str(store.session_database), "--launch", plan["launch_id"], "--tab", tab_id])


def validate_native_launch(record):
    """Admit host startup files without consuming the final execution permit."""
    from .client_files import StagingStore
    args = _launch_binding(record)
    if (args.tab != record.id or not Path(args.db).is_absolute()
            or Path(args.db).parent != Path(args.state)):
        raise StagingError("Configured launch binding changed", "unauthorized")
    store = StagingStore(Path(args.state), Path(args.auth_dir), session_database=Path(args.db), create=False)
    tree = Directory(store.root)
    try:
        with _database(tree, "metadata.sqlite3", readonly=True) as db:
            row = db.execute("SELECT plan,status FROM launches WHERE launch_id=?", (_id(args.launch, "launch_"),)).fetchone()
            if not row or row["status"] not in {"starting", "started"}:
                raise StagingError("Configured launch is not starting or started", "conflict")
            plan = _load_object(row["plan"], "launch plan")
            _require_host_plan(plan)
            _verify_launcher_identity(plan)
            if (record.command != _launcher_command(store, plan, record.id)
                    or record.cwd != plan["workspace"]["path"] or record.window_id != plan["window_id"]
                    or record.id not in [tab["id"] for tab in plan["tabs"]]):
                raise StagingError("Configured launch binding changed", "unauthorized")
            actor = _actor_from_plan(plan["actor"])
            with authorized(store.auth_directory, actor, readonly=True):
                value, revision = policy(store.root.parent)
                if revision != plan["policy_revision"]:
                    raise StagingError("Configured launch policy changed", "conflict")
                with _plan_workspace(store, plan, value) as directory:
                    directory.check()
                _validate_attachments(store, tree, db, actor, plan)
                tree.check()
    finally:
        tree.close()


def _validate_attachments(store, tree, db, actor, plan):
    for attachment in plan["attachments"]:
        row = store._row(db, actor, attachment["reference"])
        if row["status"] not in {"ready", "changed"}:
            raise StagingError("Attachment unavailable", "stale-reference")
        expected = db.execute("SELECT target FROM attachments WHERE attachment_id=? AND account_id=? AND machine_id=?",
                              (attachment["attachment_id"], actor.account_id, actor.machine_id)).fetchone()
        if not expected or expected[0] != "window:" + plan["window_id"]:
            raise StagingError("Attachment no longer authorized", "unauthorized")
        store._verify(tree, row)
        if attachment["server_path"] != str(store.root / store._location(row)):
            raise StagingError("Attachment path changed", "unsafe-storage")


@contextmanager
def _resume_authorized(store, actor):
    """Keep the original grant; fresh UI authentication cannot replace it."""
    with ExitStack() as stack:
        try:
            stack.enter_context(authorized(store.auth_directory, actor))
        except StagingError as exc:
            # This read only classifies a refusal. It never grants authority.
            directory = Directory(store.auth_directory)
            try:
                with _database(directory, "termdeck-machines-v1.sqlite3", readonly=True) as machines:
                    machine = machines.execute("SELECT * FROM machines WHERE machine_id=?", (actor.machine_id,)).fetchone()
                    if machine and machine["revoked_at_ms"] is not None:
                        raise StagingError("Original launch machine was revoked; authorize a new launch", "unauthorized") from exc
                with _database(directory, "termdeck-web-auth-v1.sqlite3", readonly=True) as auth:
                    session = auth.execute("SELECT expires_at_ms,revoked_at_ms FROM auth_sessions WHERE session_id=?", (actor.session_id,)).fetchone()
                    last = auth.execute("SELECT last_observed_at_ms FROM auth_metadata WHERE singleton=1").fetchone()[0]
                    if session and session["expires_at_ms"] <= max(int(time.time() * 1000), last):
                        raise StagingError("Original launch login expired; authorize a new launch with the current login", "unauthorized") from exc
                    if session and session["revoked_at_ms"] is not None:
                        raise StagingError("Original launch login was revoked; authorize a new launch with the current login", "unauthorized") from exc
            finally:
                directory.close()
            raise StagingError("Original launch authorization changed; authorize a new launch", "unauthorized") from exc
        yield


def _validate_resume_record(store, args, db, plan, value, expected=None):
    _verify_launcher_identity(plan)
    sessions = SessionStore(store.session_database, provision_auth_scopes=False)
    try:
        current = sessions.get(args.tab)
    finally:
        sessions.close()
    if current is None:
        raise StagingError("Configured tab is unavailable", "unauthorized")
    if "launcher" in plan and current.command != _launcher_command(store, plan, args.tab):
        raise StagingError("Configured launcher identity changed", "unauthorized")
    binding = _launch_binding(current)
    if (any(getattr(binding, key) != getattr(args, key) for key in ("state", "auth_dir", "db", "launch", "tab"))
            or not Path(args.db).is_absolute() or Path(args.db).parent != Path(args.state)
            or current.window_id != plan["window_id"] or current.cwd != plan["workspace"]["path"]
            or args.tab not in [t["id"] for t in plan["tabs"]]
            or not db.execute("SELECT 1 FROM launches WHERE launch_id=? AND status='started'", (args.launch,)).fetchone()
            or not db.execute("SELECT 1 FROM launch_runs WHERE launch_id=? AND tab_id=?", (args.launch, args.tab)).fetchone()):
        raise StagingError("Configured tab binding changed", "unauthorized")
    if expected is not None and any(getattr(current, key) != getattr(expected, key)
                                    for key in ("command", "codex_home", "auth_scope_id", "window_id", "cwd")):
        raise StagingError("Configured tab binding changed", "unauthorized")
    if plan["runtime"] != "codex" or not value["codex"]:
        raise StagingError("Configured Codex runtime changed", "unauthorized")
    expected_home = store.root.parent / "terminal-auth" / (current.auth_scope_id or "")
    if not current.auth_scope_id or current.codex_home != str(expected_home):
        raise StagingError("Configured private scope changed", "unauthorized")
    private = Directory(expected_home)
    private.close()
    from .codex_config_scope import _bound_canonical_home
    if _bound_canonical_home(expected_home) != Path(value["codex"]["account_source"]).resolve(strict=True):
        raise StagingError("Configured account source changed", "unauthorized")
    return current


@contextmanager
def _resume_context(record):
    from .client_files import StagingStore
    args = _launch_binding(record)
    if (args.tab != record.id or not Path(args.db).is_absolute()
            or Path(args.db).parent != Path(args.state)):
        raise StagingError("Configured launch binding changed", "unauthorized")
    store = StagingStore(Path(args.state), Path(args.auth_dir), session_database=Path(args.db))
    tree = Directory(store.root)
    try:
        with _database(tree, "metadata.sqlite3") as db:
            row = db.execute("SELECT plan,status FROM launches WHERE launch_id=?", (_id(args.launch, "launch_"),)).fetchone()
            if not row or row["status"] != "started":
                raise StagingError("Configured launch is not started", "conflict")
            plan = _load_object(row["plan"], "launch plan")
            _require_host_plan(plan)
            actor = _actor_from_plan(plan["actor"])
            with _resume_authorized(store, actor):
                value, revision = policy(store.root.parent)
                if revision != plan["policy_revision"] or plan["runtime"] != "codex" or not value["codex"]:
                    raise StagingError("Configured launch policy changed", "conflict")
                with _plan_workspace(store, plan, value) as directory:
                    directory.check()
                _validate_resume_record(store, args, db, plan, value, record)
                _validate_attachments(store, tree, db, actor, plan)
                _prepare_native_configuration(
                    record.codex_home, state_root=store.root.parent, auth_directory=store.auth_directory,
                    account_source=value["codex"]["account_source"], workspace=plan["workspace"]["path"],
                    write=False,
                )
                yield args, store, tree, db, plan, value
                tree.check()
    finally:
        tree.close()


def observe_resume(record, root_pid):
    """Recognize only this policy's original/resumed native process and rollout."""
    from . import resources
    from .codex_credentials import process_start
    with _resume_context(record) as (_, _, _, _, plan, value):
        executable = Path(value["codex"]["executable"]).stat()
        matches = []
        for pid in resources.process_tree(root_pid):
            before = process_start(pid)
            environment = resources._process_environment(pid) or {}
            argv = resources._process_argv(pid)
            if (before is None or environment.get("CODEX_HOME") != record.codex_home
                    or environment.get("TERMDECK_WEB_LAUNCH_ID") != plan["launch_id"]
                    or environment.get("TERMDECK_WEB_TAB_ID") != record.id):
                continue
            try:
                actual = Path(f"/proc/{pid}/exe").stat()
            except OSError:
                continue
            if (actual.st_dev, actual.st_ino) != (executable.st_dev, executable.st_ino):
                continue
            conversation = resources._primary_codex_session_id(pid)
            if not conversation or resume_identity(_RESUME_PREFIX + conversation) is None:
                continue
            if argv not in (tuple(_runtime_argv(value, plan, record.codex_home)),
                            tuple(_runtime_argv(value, plan, record.codex_home, conversation))):
                continue
            cwd = resources._process_cwd(pid)
            if cwd == plan["workspace"]["path"] and before == process_start(pid):
                matches.append((_RESUME_PREFIX + conversation, cwd))
        if len(matches) != 1:
            return True, None, None
        return True, *matches[0]


def prepare_resume(record):
    """Issue a single-use continuation under the original launch authority."""
    conversation = resume_identity(record.recovery_command)
    if conversation is None:
        raise StagingError("Configured task requires its exact conversation", "conflict")
    with _resume_context(record) as (args, _, _, db, _, _):
        db.execute(RESUMES)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) FROM launch_resumes").fetchone()[0] >= 4096:
            raise StagingError("Configured resume receipt capacity reached", "capacity")
        permit = "resume_" + uuid.uuid4().hex
        db.execute("UPDATE launch_resumes SET used=1 WHERE launch_id=? AND tab_id=? AND used=0", (args.launch, record.id))
        db.execute("INSERT INTO launch_resumes VALUES(?,?,?,?,?,?,0)",
                   (permit, args.launch, record.id, conversation, record.codex_home, record.auth_scope_id))
        db.commit()
        return record.command + " --resume-permit " + permit, permit


def cancel_resume(record, permit):
    """Invalidate only the pending internal permit when a handoff is abandoned."""
    args = _launch_binding(record)
    if args.tab != record.id or Path(args.db).parent != Path(args.state):
        raise StagingError("Configured launch binding changed", "unauthorized")
    tree = Directory(Path(args.state) / "client-files")
    try:
        with _database(tree, "metadata.sqlite3") as db:
            db.execute(RESUMES)
            db.execute("UPDATE launch_resumes SET used=1 WHERE permit_id=? AND launch_id=? AND tab_id=?",
                       (_id(permit, "resume_"), args.launch, record.id))
            db.commit()
    finally:
        tree.close()


def prepare(store,db,tree,actor,request,machine,now):
    """Called under the staging authorization fence; never takes client argv."""
    value,revision=policy(store.root.parent)
    if request.get("policy_revision")!=revision:
        raise StagingError("Workspace policy changed; refresh choices", "conflict")
    if ("workspace_id" in request) == ("workspace_path" in request):
        raise StagingError("Choose one saved workspace or enter one server directory")
    if "workspace_path" in request:
        workspace = server_workspaces.select(request["workspace_path"])
    else:
        preset=next((w for w in value["workspaces"] if w["id"]==request.get("workspace_id")),None)
        if preset is None: raise StagingError("Choose a saved server workspace or enter a server directory")
        workspace = {**server_workspaces.select(preset["path"]),
                     "id": preset["id"], "label": preset["label"], "source": "saved-workspace"}
    runtime=request.get("runtime")
    if runtime not in {"shell","codex"} or runtime == "codex" and not value["codex"]:
        raise StagingError("Unsupported or unconfigured runtime")
    name=_text(request.get("name"),256).strip()
    if not name: raise StagingError("Window name is required")
    tabs=request.get("tabs")
    if not isinstance(tabs,list) or not 1<=len(tabs)<=8:
        raise StagingError("Choose between 1 and 8 initial tabs")
    tabs=[_text(tab,128).strip() for tab in tabs]
    if not all(tabs): raise StagingError("Tab names are required")
    task=_text(request.get("task",""),8192,empty=True)
    if runtime=="shell" and task: raise StagingError("Shell task commands are not accepted")
    yolo=request.get("yolo",False)
    if not isinstance(yolo, bool) or yolo and runtime!="codex":
        raise StagingError("Invalid Codex launch mode")
    refs=request.get("attachments",[])
    if not isinstance(refs,list) or len(refs)>16: raise StagingError("Too many attachments")
    selected=[]
    for ref in refs:
        row=store._row(db,actor,ref)
        if row["status"]!="ready": raise StagingError("Select a ready current attachment", "stale-reference")
        store._verify(tree,row)
        if ref in [r["reference"] for r in selected]: raise StagingError("Duplicate attachment")
        selected.append({"reference":ref,"server_path":str(store.root/store._location(row))})
    launch_id="launch_"+uuid.uuid4().hex
    window_id="win-"+uuid.uuid4().hex
    plan={"version":_PLAN_VERSION,"execution":_EXECUTION,
          "launch_id":launch_id,"actor":asdict(actor),"policy_revision":revision,"workspace":workspace,
          "launcher":str(Path(sys.executable).resolve(strict=True)),
          "runtime":runtime,"task":task,"yolo":yolo,"window_id":window_id,"name":name,"attachments":selected,
          "tabs":[{"id":"td-"+uuid.uuid4().hex,"name":tab} for tab in tabs]}
    managed = _managed_entrypoint()
    if managed is not None:
        plan["managed_entrypoint"] = managed
    # Receipted intent precedes registry or process side effects. Replayed
    # creation reports this same identity, including incomplete launch state.
    db.execute(SCHEMA)
    db.execute("INSERT INTO launches VALUES(?,?,?,?,?,?,?,'prepared')",(launch_id,actor.account_id,actor.machine_id,actor.session_id,request["request_id"],hashlib.sha256(_json(request).encode()).hexdigest(),_json(plan)))
    for selected in plan["attachments"]:
        ref=selected["reference"]
        attachment_id="att_"+uuid.uuid4().hex
        selected["attachment_id"]=attachment_id
        db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?)",(attachment_id,actor.account_id,actor.machine_id,ref["object_id"],ref["generation"],ref["digest"],"window:"+window_id,now))
    db.execute("UPDATE launches SET plan=? WHERE launch_id=?",(_json(plan),launch_id))
    result={"ok":True,"window_id":window_id,"launch_id":launch_id,"status":"prepared","tabs":[t["id"] for t in plan["tabs"]]}
    result["attachments"]=[{"attachment_id":a["attachment_id"],"reference":a["reference"],"target":"window:"+window_id} for a in plan["attachments"]]
    store._receipt(db,actor,request,result)
    db.commit()
    return result


def start(store,actor,result,*,backend=None):
    """Register only the exact intent's own records, then ask admitted tmux."""
    from .tmux_backend import TmuxBackend
    with authorized(store.auth_directory,actor):
        tree=Directory(store.root)
        try:
            with _database(tree,"metadata.sqlite3") as db:
                row=db.execute("SELECT plan,status FROM launches WHERE launch_id=? AND account_id=? AND machine_id=? AND session_id=?",(result["launch_id"],actor.account_id,actor.machine_id,actor.session_id)).fetchone()
                if not row: raise StagingError("Launch is unavailable", "unauthorized")
                plan=_load_object(row["plan"], "launch plan")
                _require_host_plan(plan)
                _verify_launcher_identity(plan)
                if row["status"]!="prepared":
                    if row["status"]=="started": return dict(result,status="started")
                    message = ("Window creation is in progress or was interrupted; retry its status before explicit recovery"
                               if row["status"] == "starting" else
                               "Window exists but launch did not finish; inspect its tabs before explicit recovery")
                    return dict(result,ok=False,status=row["status"],error=message,code="conflict")
                value,revision=policy(store.root.parent)
                if revision!=plan["policy_revision"]: raise StagingError("Launch policy changed", "conflict")
                with _plan_workspace(store, plan, value) as directory:
                    directory.check()
                # Once marked starting, replay never respawns a dead/finished
                # task. An interrupted partial start requires explicit recovery.
                db.execute("UPDATE launches SET status='starting' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                sessions=SessionStore(store.session_database,provision_auth_scopes=False)
                try:
                    sessions.remember_window(plan["window_id"],plan["name"])
                    for index,tab in enumerate(plan["tabs"]):
                        command=_launcher_command(store, plan, tab["id"])
                        record=SessionRecord(id=tab["id"],name=tab["name"],tag="web-agent",command=command,cwd=plan["workspace"]["path"],tmux_name="termdeck_web_"+tab["id"].replace("-","_"),created_at=utc_now(),last_opened_at=utc_now(),window_id=plan["window_id"],window_title=plan["name"],tab_index=index,restart_policy="never")
                        if sessions.get(record.id) is not None: raise StagingError("Launch registry identity conflicts", "conflict")
                        sessions.upsert(record)
                        (backend or TmuxBackend()).create_or_resume(record)
                        # ubs:ignore[py.security.mass-assignment] -- only this fixed server-owned launch status is written; request keys never enter this model update.
                        sessions.update(record.id,status="running")
                    db.execute("UPDATE launches SET status='started' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                except Exception:
                    db.execute("UPDATE launches SET status='failed' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                    return dict(result,ok=False,status="failed",error="Window was saved but a terminal could not start; inspect the saved window",code="conflict")
                finally: sessions.close()
                return dict(result,status="started")
        finally: tree.close()


def _native_shell_argv():
    """Use the host user's shell with its ordinary interactive startup files."""
    candidates = [os.environ.get("SHELL", "")]
    try:
        candidates.append(pwd.getpwuid(os.geteuid()).pw_shell)
    except (KeyError, OSError):
        pass
    candidates.append("/bin/sh")
    for candidate in candidates:
        if not candidate or not Path(candidate).is_absolute():
            continue
        try:
            path = _host_path(str(Path(candidate).resolve(strict=True)))
        except (OSError, RuntimeError, StagingError):
            continue
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path), "-i"]
    raise StagingError("Host user shell is unavailable", "unavailable-runtime")


def _scope_environment(record):
    from .tmux_backend import auth_lifetime_gate_root
    home = record.codex_home
    private_bin = str(Path(home) / "bin")
    host_path = os.environ.get("PATH", os.defpath)
    return {"CODEX_HOME": home, "TERMDECK_AUTH_SCOPE_ID": record.auth_scope_id,
            "CODEX_TERMDECK_GATE_ROOT": str(auth_lifetime_gate_root(record)),
            "PATH": private_bin + (":" + host_path if host_path else "")}


def _shell_launch(store, plan, tab_id):
    runtime = _native_shell_argv()
    sessions = SessionStore(store.session_database, provision_auth_scopes=False)
    with closing(sessions):
        record = sessions.get(tab_id)
    if record is None or not record.codex_home:
        return runtime, {}
    # New shell tabs are unscoped. An account scope deliberately assigned later
    # remains bound to its exact saved tab, even if startup files changed env.
    expected_home = store.root.parent / "terminal-auth" / (record.auth_scope_id or "")
    if (not record.auth_scope_id or record.codex_home != str(expected_home)
            or record.command != _launcher_command(store, plan, tab_id)
            or record.cwd != plan["workspace"]["path"] or record.window_id != plan["window_id"]):
        raise StagingError("Configured private scope changed", "unauthorized")
    private = Directory(expected_home)
    private.close()
    if Path(runtime[0]).name.casefold() == "bash":
        from .codex_config_scope import prepare_private_bash_launch_command, CodexConfigScopeError
        try:
            runtime = shlex.split(prepare_private_bash_launch_command(
                expected_home, auth_scope_id=record.auth_scope_id, shell_path=runtime[0]))
        except CodexConfigScopeError:
            raise StagingError("Private shell scope could not be prepared", "configuration-conflict") from None
    return runtime, _scope_environment(record)


def execute(args):
    from .client_files import StagingStore
    store=StagingStore(Path(args.state),Path(args.auth_dir),session_database=Path(args.db))
    tree=Directory(store.root)
    try:
        with _database(tree,"metadata.sqlite3") as db:
            row=db.execute("SELECT plan FROM launches WHERE launch_id=?",(_id(args.launch,"launch_"),)).fetchone()
            if not row: raise StagingError("Launch is unavailable", "unauthorized")
            plan=_load_object(row["plan"], "launch plan")
            _require_host_plan(plan)
            _verify_launcher_identity(plan)
        actor=_actor_from_plan(plan["actor"])
        permit = getattr(args, "resume_permit", None)
        resume_record = None
        with (_resume_authorized(store, actor) if permit else authorized(store.auth_directory, actor)):
            value,revision=policy(store.root.parent)
            if revision!=plan["policy_revision"]: raise StagingError("Launch policy changed", "conflict")
            with _plan_workspace(store, plan, value) as directory:
                directory.check()
            if args.tab not in [t["id"] for t in plan["tabs"]]: raise StagingError("Tab is not authorized", "unauthorized")
            conversation = None
            with _database(tree,"metadata.sqlite3") as db:
                db.execute(RUNS)
                _validate_attachments(store, tree, db, actor, plan)
                if permit:
                    resume_record = _validate_resume_record(store, args, db, plan, value)
                    db.execute(RESUMES)
                    db.execute("BEGIN IMMEDIATE")
                    continuation = db.execute("SELECT * FROM launch_resumes WHERE permit_id=? AND launch_id=? AND tab_id=? AND used=0",
                                              (_id(permit, "resume_"), plan["launch_id"], args.tab)).fetchone()
                    if (not continuation or continuation["codex_home"] != resume_record.codex_home
                            or continuation["scope_id"] != resume_record.auth_scope_id):
                        raise StagingError("Resume permit is unavailable or consumed", "conflict")
                    conversation = continuation["conversation_id"]
                    consumed = db.execute("UPDATE launch_resumes SET used=1 WHERE permit_id=? AND used=0", (permit,))
                    if consumed.rowcount != 1:
                        raise StagingError("Resume permit was already consumed", "conflict")
                else:
                    if db.execute("SELECT 1 FROM launch_runs WHERE launch_id=? AND tab_id=?",(plan["launch_id"],args.tab)).fetchone():
                        raise StagingError("Launch permit was already consumed", "conflict")
                    db.execute("INSERT INTO launch_runs VALUES(?,?)",(plan["launch_id"],args.tab))
                db.commit()
            runtime, scope_environment = _shell_launch(store, plan, args.tab) if plan["runtime"] == "shell" else (None, {})
            if plan["runtime"]=="codex":
                if not value["codex"]: raise StagingError("TermDeck Codex runtime unavailable")
                sessions=SessionStore(store.session_database,account_source=value["codex"]["account_source"],provision_auth_scopes=False)
                with closing(sessions):
                    record=sessions.ensure_auth_scope(args.tab, private_history=True)
                    codex_home=record.codex_home
                _validate_shared_assets(
                    codex_home, state_root=store.root.parent, auth_directory=store.auth_directory,
                    account_source=value["codex"]["account_source"], workspace=plan["workspace"]["path"],
                )
                _prepare_native_configuration(
                    codex_home, state_root=store.root.parent, auth_directory=store.auth_directory,
                    account_source=value["codex"]["account_source"], workspace=plan["workspace"]["path"],
                )
                _prepare_private_log_directory(codex_home)
                credential=Path(codex_home)/"auth.json"
                if credential.is_symlink():
                    from .codex_auth import read_auth_state
                    state=read_auth_state(codex_home,account_source=value["codex"]["account_source"])
                    if not state.canonical_binding_ready:
                        raise StagingError("Private account binding needs repair", "unauthorized")
                if resume_record and (record.codex_home != resume_record.codex_home or record.auth_scope_id != resume_record.auth_scope_id):
                    raise StagingError("Resume private scope changed", "unauthorized")
                scope_environment = {**_scope_environment(record),
                    "TERMDECK_WEB_LAUNCH_ID": plan["launch_id"], "TERMDECK_WEB_TAB_ID": record.id}
                runtime = _runtime_argv(value, plan, codex_home, conversation)
            with _plan_workspace(store, plan, value) as directory:
                tree.check()
                with directory.entered():
                    environment = {**os.environ, **scope_environment, "PWD": str(directory.path)}
                    expected_runtime_sha = value["codex"]["sha256"] if plan["runtime"] == "codex" else None
                    with _pinned_executable(runtime, expected_runtime_sha) as (descriptor, argv):
                        # ubs:ignore[python.taint.command] -- os.execve receives the pinned O_NOFOLLOW file descriptor; its inode, owner, mode, path and policy digest were rechecked above, with no shell parsing.
                        os.execve(descriptor, argv, environment)
    finally: tree.close()


def add_arguments(parser):
    for name in ("state","auth-dir","db","launch","tab"): parser.add_argument("--"+name,required=True)
    parser.add_argument("--resume-permit")


def command(args):
    try: execute(args)
    except (StagingError,OSError,ValueError) as exc:
        if isinstance(exc, StagingError):
            print("TermDeck native task could not start: " + str(exc), file=sys.stderr)
            return 78
        print("TermDeck native task could not start; review its launch policy and attachment authorization.",file=sys.stderr)
        return 78
    return 0


def main():
    import argparse
    parser=argparse.ArgumentParser()
    add_arguments(parser)
    return command(parser.parse_args())


if __name__=="__main__": raise SystemExit(main())
