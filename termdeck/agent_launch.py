"""Server-owned workspace policy and confined, immutable attachment launches."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
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


def _within(path, root):
    return path == root or root in path.parents


def _runtime_roots(state_root):
    state_root = Path(state_root)
    return (state_root, *(p for p in state_root.parents if p.name == ".termdeck"))


def _validate_workspace_grant(workspace, state_root, *, protected=()):
    workspace = Path(workspace)
    if any(name in workspace.parts for name in _MASKED_NAMES):
        raise StagingError("Protected storage cannot be a workspace", "unsafe-storage")
    for root in (*_runtime_roots(state_root), *(Path(p) for p in protected)):
        if _within(workspace, root):
            raise StagingError("Protected storage cannot be a workspace", "unsafe-storage")
        if _within(root, workspace) and not any(
            _within(root, workspace / name) for name in _MASKED_NAMES
        ):
            raise StagingError("Workspace would expose protected storage", "unsafe-storage")


def _workspace_protected(store, value):
    return [store.auth_directory] + ([value["codex"]["account_source"]] if value["codex"] else [])


@contextmanager
def _plan_workspace(store, plan, value, *, inspect=False):
    workspace = plan["workspace"]
    protected = _workspace_protected(store, value)
    if "pin" in workspace:
        with server_workspaces.open_workspace(workspace, state_root=store.root.parent,
                                               protected=protected, inspect=inspect) as directory:
            yield directory
    else:
        # Existing prepared receipts retain their original explicit grant and
        # checks. They never acquire a different saved choice's current path.
        _host_path(workspace["path"], directory=True)
        _validate_workspace_grant(workspace["path"], store.root.parent, protected=protected)
        directory = server_workspaces.WorkspaceDirectory(workspace["path"], state_root=store.root.parent,
                                                         protected=protected, inspect=inspect)
        try:
            yield directory
        finally:
            directory.close()


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


def _shared_asset_mounts(home, *, state_root, auth_directory, account_source, workspace):
    """Keep prepared links reachable without importing their parent directories.

    Only the preparation catalogue supplies asset names. Nested links and paths
    mentioned in configuration never acquire additional filesystem authority.
    """
    from .codex_config_scope import _canonical_assets, CodexConfigScopeError
    try:
        assets = [asset for asset in _canonical_assets(Path(account_source)) if asset[0] != ".env"]
        checks = dict(state_root=state_root, auth_directory=auth_directory,
                      account_source=account_source, private_home=home, workspace=workspace,
                      shared_names={name for name, _path, _directory in assets})
        mounts = {}
        for name, canonical, _directory in assets:
            private = Path(home) / name
            if not private.is_symlink():
                continue
            target = Path(_shared_asset_path(private, **checks))
            if target != canonical.resolve(strict=True):
                raise StagingError("Shared asset binding changed", "unsafe-storage")
            # Existing valid links can point directly at a target or use a
            # different alias. Do not rewrite any host or private symlink.
            alias = Path(os.path.abspath(private.parent / os.readlink(private)))
            for destination in (target, canonical, alias):
                _validate_shared_location(destination, **checks)
                previous = mounts.setdefault(destination, target)
                if previous != target:
                    raise StagingError("Shared asset mount destinations conflict", "unsafe-storage")
        ordered = sorted(mounts.items(), key=lambda item: (len(item[0].parts), str(item[0])))
        result = []
        for destination, target in ordered:
            for parent_target, parent_destination in result:
                if _within(destination, parent_destination):
                    relative = destination.relative_to(parent_destination)
                    if target != parent_target / relative:
                        raise StagingError("Shared asset mount destinations overlap", "unsafe-storage")
                    break  # Already exposed at this exact location by its parent.
            else:
                result.append((target, destination))
        return tuple((str(target), str(destination)) for target, destination in result)
    except (OSError, RuntimeError, CodexConfigScopeError) as exc:
        raise StagingError("Shared asset catalogue cannot be validated", "unsafe-storage") from exc


def _prepare_private_log_directory(home):
    """Create only a direct, owned private directory; refuse existing aliases."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = log = None
    try:
        root = os.open(home, flags)
        info = os.fstat(root)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StagingError("Private Codex home has unsafe authority", "unsafe-storage")
        try:
            os.mkdir("log", 0o700, dir_fd=root)
        except FileExistsError:
            pass
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


def policy(state_root):
    tree=Directory(state_root)
    try:
        try:
            with tree.file(POLICY) as (fd,_):
                raw=os.read(fd,65537)
        except FileNotFoundError:
            return {"version":1,"workspaces":[],"codex":None}, "unconfigured"
        if len(raw)>65536: raise StagingError("Launch policy is too large", "unsafe-storage")
        value=json.loads(raw)
        if not isinstance(value,dict) or set(value)!={"version","workspaces","codex"} or value["version"]!=1:
            raise StagingError("Invalid launch policy", "unsafe-storage")
        if not isinstance(value["workspaces"],list) or len(value["workspaces"])>64:
            raise StagingError("Invalid workspace policy", "unsafe-storage")
        seen=set()
        for workspace in value["workspaces"]:
            if not isinstance(workspace,dict) or set(workspace)!={"id","label","path"}:
                raise StagingError("Invalid workspace policy", "unsafe-storage")
            _id(workspace["id"],"ws_")
            _text(workspace["label"],256)
            directory = server_workspaces.WorkspaceDirectory(workspace["path"], state_root=state_root, inspect=False)
            try:
                workspace["path"] = str(directory.path)
            finally:
                directory.close()
            if workspace["id"] in seen: raise StagingError("Duplicate workspace identity", "unsafe-storage")
            seen.add(workspace["id"])
        codex=value["codex"]
        if codex is not None:
            if not isinstance(codex,dict) or set(codex)!={"executable","sha256","account_source"}:
                raise StagingError("Invalid TermDeck Codex runtime policy", "unsafe-storage")
            executable=_host_path(codex["executable"])
            _id(codex["sha256"],"",64)
            if hashlib.sha256(executable.read_bytes()).hexdigest()!=codex["sha256"] or not os.access(executable,os.X_OK):
                raise StagingError("TermDeck Codex runtime identity changed", "unsafe-storage")
            source = _host_path(codex["account_source"],directory=True)
            for workspace in value["workspaces"]:
                directory = server_workspaces.WorkspaceDirectory(workspace["path"], state_root=state_root,
                                                                 protected=(source,), inspect=False)
                directory.close()
        return value,hashlib.sha256(raw).hexdigest()
    finally: tree.close()


def options(state_root):
    value,digest=policy(state_root)
    return {"ok":True,"policy_revision":digest,"workspaces":value["workspaces"],"server_directory":True,
            "runtimes":[{"id":"shell","label":"Confined shell"}]+([{"id":"codex","label":"Codex (TermDeck private account)"}] if value["codex"] else [])}


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
    if (record.tag != "web-agent" or record.external or len(parts) != 14
            or parts[0] != "exec" or not Path(parts[1]).is_absolute()
            or parts[2:5] != ["-m", "termdeck.agent_launch", "--state"]
            or parts[6::2] != ["--auth-dir", "--db", "--launch", "--tab"]):
        raise StagingError("Configured launch binding is unavailable", "unauthorized")
    args = SimpleNamespace(state=parts[5], auth_dir=parts[7], db=parts[9], launch=parts[11], tab=parts[13])
    # The fixed entrypoint has fourteen words; reject trailing decoration.
    return args


def _runtime_argv(value, plan, home, conversation=None):
    argv = [value["codex"]["executable"], "-c", f"sqlite_home={json.dumps(home)}",
            "-c", f"log_dir={json.dumps(str(Path(home) / 'log'))}",
            "--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
    if conversation:
        return [*argv, "resume", conversation]
    prompt = plan["task"] + "\nAuthorized immutable files staged on the TermDeck host:\n" + "\n".join(a["server_path"] for a in plan["attachments"])
    return [*argv, "--", prompt]


def _launcher_command(store, plan, tab_id):
    return "exec " + shlex.join([plan.get("launcher", sys.executable), "-m", "termdeck.agent_launch",
        "--state", str(store.root.parent), "--auth-dir", str(store.auth_directory),
        "--db", str(store.session_database), "--launch", plan["launch_id"], "--tab", tab_id])


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
            plan = json.loads(row["plan"])
            actor = Actor(**plan["actor"])
            with _resume_authorized(store, actor):
                value, revision = policy(store.root.parent)
                if revision != plan["policy_revision"] or plan["runtime"] != "codex" or not value["codex"]:
                    raise StagingError("Configured launch policy changed", "conflict")
                with _plan_workspace(store, plan, value) as directory:
                    directory.check()
                _validate_resume_record(store, args, db, plan, value, record)
                _validate_attachments(store, tree, db, actor, plan)
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
        workspace = server_workspaces.select(request["workspace_path"], state_root=store.root.parent,
                                              protected=_workspace_protected(store, value))
    else:
        preset=next((w for w in value["workspaces"] if w["id"]==request.get("workspace_id")),None)
        if preset is None: raise StagingError("Choose a saved server workspace or enter a server directory")
        workspace = {**server_workspaces.select(preset["path"], state_root=store.root.parent,
                                                protected=_workspace_protected(store, value)),
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
    plan={"launch_id":launch_id,"actor":asdict(actor),"policy_revision":revision,"workspace":workspace,
          "launcher":str(Path(sys.executable).resolve(strict=True)),
          "runtime":runtime,"task":task,"window_id":window_id,"name":name,"attachments":selected,
          "tabs":[{"id":"td-"+uuid.uuid4().hex,"name":tab} for tab in tabs]}
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
                plan=json.loads(row["plan"])
                if row["status"]!="prepared":
                    if row["status"]=="started": return dict(result,status="started")
                    return dict(result,ok=False,status=row["status"],error="Window exists but launch did not finish; inspect its tabs before explicit recovery",code="conflict")
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
                        sessions.update(record.id,status="running")
                    db.execute("UPDATE launches SET status='started' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                except Exception:
                    db.execute("UPDATE launches SET status='failed' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                    return dict(result,ok=False,status="failed",error="Window was saved but a terminal could not start; inspect the saved window",code="conflict")
                finally: sessions.close()
                return dict(result,status="started")
        finally: tree.close()


def sandbox_argv(workspace,attachments,*,runtime_argv,codex_home=None,asset_paths=(),asset_mounts=(),scope_environment=None,workspace_directory=None):
    """A mount namespace contains system libraries and explicit task authority."""
    args=["/usr/bin/bwrap","--die-with-parent","--unshare-all","--share-net","--new-session","--clearenv",
          "--ro-bind","/usr","/usr","--symlink","usr/bin","/bin","--symlink","usr/lib","/lib","--symlink","usr/lib","/lib64",
          "--proc","/proc","--dev","/dev","--tmpfs","/tmp","--dir","/run","--dir","/run/termdeck-home",
          "--setenv","HOME","/run/termdeck-home","--setenv","PATH","/usr/bin","--setenv","TERM","xterm-256color",
          "--setenv","LANG","C.UTF-8"]
    for name in ("resolv.conf","hosts","nsswitch.conf","ssl/certs"):
        source=Path("/etc")/name
        if source.exists(): args.extend(["--ro-bind",str(source),str(source)])
    if workspace_directory is not None:
        args.extend(workspace_directory.mount_arguments())
    else:
        args.extend(["--bind",workspace,workspace])
        for name in _MASKED_NAMES:
            if (Path(workspace)/name).exists(): args.extend(["--tmpfs",str(Path(workspace)/name)])
    for path in attachments: args.extend(["--ro-bind",path,path])
    if codex_home:
        args.extend(["--bind",codex_home,codex_home,"--setenv","CODEX_HOME",codex_home])
    for key, value in (scope_environment or {}).items():
        args.extend(["--setenv", key, value])
    for source in asset_paths: args.extend(["--ro-bind",source,source])
    for source, destination in asset_mounts: args.extend(["--ro-bind",source,destination])
    args.extend(["--chdir",workspace,"--",*runtime_argv])
    return args


def execute(args):
    from .client_files import StagingStore
    store=StagingStore(Path(args.state),Path(args.auth_dir),session_database=Path(args.db))
    tree=Directory(store.root)
    try:
        with _database(tree,"metadata.sqlite3") as db:
            row=db.execute("SELECT plan FROM launches WHERE launch_id=?",(_id(args.launch,"launch_"),)).fetchone()
            if not row: raise StagingError("Launch is unavailable", "unauthorized")
            plan=json.loads(row["plan"])
            if "launcher" in plan and plan["launcher"] != str(Path(sys.executable).resolve(strict=True)):
                raise StagingError("Configured launcher identity changed", "unauthorized")
        actor=Actor(**plan["actor"])
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
            runtime=["/usr/bin/bash","--noprofile","--norc","-i"]
            codex_home=None
            assets=[]
            asset_mounts=()
            scope_environment = {}
            if plan["runtime"]=="codex":
                if not value["codex"]: raise StagingError("TermDeck Codex runtime unavailable")
                sessions=SessionStore(store.session_database,account_source=value["codex"]["account_source"],provision_auth_scopes=False)
                try:
                    record=sessions.ensure_auth_scope(args.tab, private_history=True)
                    codex_home=record.codex_home
                finally: sessions.close()
                executable=value["codex"]["executable"]
                if not executable.startswith("/usr/"): assets.append(executable)
                # Resolve only explicitly shared non-auth assets. Neither the
                # account catalogue nor another terminal home is mounted.
                asset_mounts = _shared_asset_mounts(
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
                    # Only the selected credential is readable. Codex may
                    # replace its private auth.json during refresh; no account
                    # catalogue or another terminal's credential is mounted.
                    assets.append(str(credential.resolve(strict=True)))
                if resume_record and (record.codex_home != resume_record.codex_home or record.auth_scope_id != resume_record.auth_scope_id):
                    raise StagingError("Resume private scope changed", "unauthorized")
                from .tmux_backend import auth_lifetime_gate_root
                scope_environment = {"CODEX_HOME": codex_home, "TERMDECK_AUTH_SCOPE_ID": record.auth_scope_id,
                    "CODEX_TERMDECK_GATE_ROOT": str(auth_lifetime_gate_root(record)),
                    "TERMDECK_WEB_LAUNCH_ID": plan["launch_id"], "TERMDECK_WEB_TAB_ID": record.id,
                    "PATH": str(Path(codex_home) / "bin") + ":/usr/bin"}
                runtime = _runtime_argv(value, plan, codex_home, conversation)
            with _plan_workspace(store, plan, value, inspect=True) as directory:
                command=sandbox_argv(plan["workspace"]["path"],[a["server_path"] for a in plan["attachments"]],runtime_argv=runtime,codex_home=codex_home,asset_paths=assets,asset_mounts=asset_mounts,scope_environment=scope_environment,workspace_directory=directory)
                tree.check()
                directory.check()
                os.set_inheritable(directory.fd, True)
                os.execve(command[0],command,{"PATH":"/usr/bin", **scope_environment})
    finally: tree.close()


def main():
    import argparse
    parser=argparse.ArgumentParser()
    for name in ("state","auth-dir","db","launch","tab"): parser.add_argument("--"+name,required=True)
    parser.add_argument("--resume-permit")
    try: execute(parser.parse_args())
    except (StagingError,OSError,ValueError):
        print("TermDeck confined task could not start; review its launch policy and attachment authorization.",file=sys.stderr)
        return 78
    return 0


if __name__=="__main__": raise SystemExit(main())
