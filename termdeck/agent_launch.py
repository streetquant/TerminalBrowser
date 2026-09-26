"""Server-owned workspace policy and confined, immutable attachment launches."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid

from .client_files import Actor, Directory, StagingError, _database, _id, _json, _text, authorized
from .models import SessionRecord
from .session_store import SessionStore, utc_now


POLICY = "web-launch-policy.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS launches(launch_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,machine_id TEXT NOT NULL,
 session_id TEXT NOT NULL,request_id TEXT NOT NULL,body_digest TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,
 UNIQUE(account_id,machine_id,session_id,request_id));
"""
RUNS = "CREATE TABLE IF NOT EXISTS launch_runs(launch_id TEXT NOT NULL,tab_id TEXT NOT NULL,PRIMARY KEY(launch_id,tab_id));"
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


def _shared_asset_path(path, *, state_root, auth_directory, account_source,
                       private_home, workspace, shared_names):
    """Check the resolved grant, not merely the shared asset's link name."""
    target = Path(path).resolve(strict=True)
    target = _host_path(str(target), directory=target.is_dir())
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
    return str(target)


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
            root=_host_path(workspace["path"],directory=True)
            if root == Path("/") or root == Path.home() or root == Path("/tmp"):
                raise StagingError("Workspace grants excessive host access", "unsafe-storage")
            _validate_workspace_grant(root, state_root)
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
                _validate_workspace_grant(workspace["path"], state_root, protected=(source,))
        return value,hashlib.sha256(raw).hexdigest()
    finally: tree.close()


def options(state_root):
    value,digest=policy(state_root)
    return {"ok":True,"policy_revision":digest,"workspaces":value["workspaces"],
            "runtimes":[{"id":"shell","label":"Confined shell"}]+([{"id":"codex","label":"Codex (TermDeck private account)"}] if value["codex"] else [])}


def prepare(store,db,tree,actor,request,machine,now):
    """Called under the staging authorization fence; never takes client argv."""
    value,revision=policy(store.root.parent)
    if request.get("policy_revision")!=revision:
        raise StagingError("Workspace policy changed; refresh choices", "conflict")
    workspace=next((w for w in value["workspaces"] if w["id"]==request.get("workspace_id")),None)
    if workspace is None: raise StagingError("Choose an approved server workspace")
    workspace_path=Path(workspace["path"])
    protected=[store.auth_directory]
    if value["codex"]: protected.append(Path(value["codex"]["account_source"]))
    _validate_workspace_grant(workspace_path, store.root.parent, protected=protected)
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
                protected = [store.auth_directory]
                if value["codex"]: protected.append(Path(value["codex"]["account_source"]))
                _validate_workspace_grant(plan["workspace"]["path"], store.root.parent, protected=protected)
                # Once marked starting, replay never respawns a dead/finished
                # task. An interrupted partial start requires explicit recovery.
                db.execute("UPDATE launches SET status='starting' WHERE launch_id=?",(plan["launch_id"],)); db.commit()
                sessions=SessionStore(store.session_database,provision_auth_scopes=False)
                try:
                    sessions.remember_window(plan["window_id"],plan["name"])
                    for index,tab in enumerate(plan["tabs"]):
                        command="exec "+shlex.join([sys.executable,"-m","termdeck.agent_launch","--state",str(store.root.parent),"--auth-dir",str(store.auth_directory),"--db",str(store.session_database),"--launch",plan["launch_id"],"--tab",tab["id"]])
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


def sandbox_argv(workspace,attachments,*,runtime_argv,codex_home=None,asset_paths=()):
    """A mount namespace contains system libraries and explicit task authority."""
    args=["/usr/bin/bwrap","--die-with-parent","--unshare-all","--share-net","--new-session","--clearenv",
          "--ro-bind","/usr","/usr","--symlink","usr/bin","/bin","--symlink","usr/lib","/lib","--symlink","usr/lib","/lib64",
          "--proc","/proc","--dev","/dev","--tmpfs","/tmp","--dir","/run","--dir","/home/task",
          "--setenv","HOME","/home/task","--setenv","PATH","/usr/bin","--setenv","TERM","xterm-256color",
          "--setenv","LANG","C.UTF-8","--bind",workspace,workspace]
    for name in ("resolv.conf","hosts","nsswitch.conf","ssl/certs"):
        source=Path("/etc")/name
        if source.exists(): args.extend(["--ro-bind",str(source),str(source)])
    for name in _MASKED_NAMES:
        if (Path(workspace)/name).exists(): args.extend(["--tmpfs",str(Path(workspace)/name)])
    for path in attachments: args.extend(["--ro-bind",path,path])
    if codex_home:
        args.extend(["--bind",codex_home,codex_home,"--setenv","CODEX_HOME",codex_home])
    for source in asset_paths: args.extend(["--ro-bind",source,source])
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
        actor=Actor(**plan["actor"])
        with authorized(store.auth_directory,actor):
            value,revision=policy(store.root.parent)
            if revision!=plan["policy_revision"]: raise StagingError("Launch policy changed", "conflict")
            protected = [store.auth_directory]
            if value["codex"]: protected.append(Path(value["codex"]["account_source"]))
            _validate_workspace_grant(plan["workspace"]["path"], store.root.parent, protected=protected)
            if args.tab not in [t["id"] for t in plan["tabs"]]: raise StagingError("Tab is not authorized", "unauthorized")
            with _database(tree,"metadata.sqlite3") as db:
                db.execute(RUNS)
                if db.execute("SELECT 1 FROM launch_runs WHERE launch_id=? AND tab_id=?",(plan["launch_id"],args.tab)).fetchone():
                    raise StagingError("Launch permit was already consumed", "conflict")
                for attachment in plan["attachments"]:
                    row=store._row(db,actor,attachment["reference"])
                    if row["status"] not in {"ready","changed"}: raise StagingError("Attachment unavailable", "stale-reference")
                    expected=db.execute("SELECT target FROM attachments WHERE attachment_id=? AND account_id=? AND machine_id=?",(attachment["attachment_id"],actor.account_id,actor.machine_id)).fetchone()
                    if not expected or expected[0]!="window:"+plan["window_id"]: raise StagingError("Attachment no longer authorized", "unauthorized")
                    store._verify(tree,row)
                    if attachment["server_path"]!=str(store.root/store._location(row)): raise StagingError("Attachment path changed", "unsafe-storage")
                db.execute("INSERT INTO launch_runs VALUES(?,?)",(plan["launch_id"],args.tab)); db.commit()
            runtime=["/usr/bin/bash","--noprofile","--norc","-i"]
            codex_home=None
            assets=[]
            if plan["runtime"]=="codex":
                if not value["codex"]: raise StagingError("TermDeck Codex runtime unavailable")
                sessions=SessionStore(store.session_database,account_source=value["codex"]["account_source"],provision_auth_scopes=False)
                try:
                    record=sessions.ensure_auth_scope(args.tab)
                    codex_home=record.codex_home
                finally: sessions.close()
                executable=value["codex"]["executable"]
                if not executable.startswith("/usr/"): assets.append(executable)
                # Resolve only explicitly shared non-auth assets. Neither the
                # account catalogue nor another terminal home is mounted.
                from .codex_config_scope import _SHARED_FILES, _SHARED_DIRECTORIES
                shared_names = set((*_SHARED_FILES, *_SHARED_DIRECTORIES)) - {".env"}
                for name in sorted(shared_names):
                    path=Path(codex_home)/name
                    if path.is_symlink() and path.exists():
                        assets.append(_shared_asset_path(
                            path, state_root=store.root.parent, auth_directory=store.auth_directory,
                            account_source=value["codex"]["account_source"], private_home=codex_home,
                            workspace=plan["workspace"]["path"], shared_names=shared_names,
                        ))
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
                prompt=plan["task"]+"\nAuthorized immutable files staged on the TermDeck host:\n"+"\n".join(a["server_path"] for a in plan["attachments"])
                runtime=[executable,"-c",f"sqlite_home={json.dumps(codex_home)}","--sandbox","workspace-write","--ask-for-approval","on-request",prompt]
            command=sandbox_argv(plan["workspace"]["path"],[a["server_path"] for a in plan["attachments"]],runtime_argv=runtime,codex_home=codex_home,asset_paths=assets)
            tree.check()
            os.execve(command[0],command,{"PATH":"/usr/bin"})
    finally: tree.close()


def main():
    import argparse
    parser=argparse.ArgumentParser()
    for name in ("state","auth-dir","db","launch","tab"): parser.add_argument("--"+name,required=True)
    try: execute(parser.parse_args())
    except (StagingError,OSError,ValueError):
        print("TermDeck confined task could not start; review its launch policy and attachment authorization.",file=sys.stderr)
        return 78
    return 0


if __name__=="__main__": raise SystemExit(main())
