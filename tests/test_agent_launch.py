import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys
import uuid

import pytest

from termdeck import agent_launch as launch
from termdeck import client_files as cf
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend
from test_client_files import stage, call, upload, path, ordinary_workspace_root  # noqa: F401


def _fixture_object(source, *, context):
    """Parse one generated JSON object with a useful failure at the fixture edge."""
    try:
        raw = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        pytest.fail(f"{context} is not valid fixture JSON: {error}", pytrace=False)
    if not isinstance(value, dict):
        pytest.fail(f"{context} must be a JSON object", pytrace=False)
    return value


@pytest.fixture
def configured(stage, ordinary_workspace_root):
    store,actor=stage
    workspace=ordinary_workspace_root/"workspace"
    workspace.mkdir(mode=0o700)
    config={"version":1,"workspaces":[{"id":"ws_"+"a"*32,"label":"Approved project","path":str(workspace)}],"codex":None}
    policy=store.root.parent/launch.POLICY
    policy.write_text(json.dumps(config)); policy.chmod(0o600)
    return store,actor,workspace,config


def request(configured,**changes):
    store,actor,workspace,config=configured
    options=call((store,actor),"launch-options")
    return {"operation":"create-window","request_id":uuid.uuid4().hex,"name":"Agent window","policy_revision":options["policy_revision"],"workspace_id":config["workspaces"][0]["id"],"runtime":"shell","tabs":["One","Two"],"attachments":[],**changes}


class Backend:
    def __init__(self): self.records=[]
    def create_or_resume(self,record): self.records.append(record)


def test_launch_receipt_registry_replay_and_preservation(configured):
    store,actor,workspace,_=configured
    sessions=SessionStore(store.session_database)
    old=sessions.create("existing","default","sleep 999",str(workspace),window_id="old")
    sessions.close()
    obj=upload((store,actor))
    req=request(configured,attachments=[obj["reference"]])
    first=store.handle(actor,req)
    repeated=store.handle(actor,req)
    assert first["launch_id"] == repeated["launch_id"]
    backend=Backend()
    result=launch.start(store,actor,first,backend=backend)
    assert result["status"] == "started"
    assert len(backend.records)==2
    launch.start(store,actor,repeated,backend=backend)
    assert len(backend.records)==2
    sessions=SessionStore(store.session_database)
    try:
        assert sessions.get(old.id)==old
        assert {r.id for r in sessions.list(window_id=first["window_id"])}==set(first["tabs"])
        assert all(r.restart_policy=="never" and r.tag=="web-agent" for r in backend.records)
    finally: sessions.close()
    with pytest.raises(cf.StagingError): call((store,actor),"remove",reference=obj["reference"])


@pytest.mark.parametrize("changes", [{"workspace_id":"/etc"},{"runtime":"bash -c echo bad"},{"runtime":"codex"},{"task":"touch /tmp/bad"},{"command":"sh"},{"tabs":[]},{"tabs":["x"]*9},{"name":"bad\x00name"},{"policy_revision":"stale"}])
def test_invalid_launch_never_changes_registry(configured,changes):
    store,actor,_,_=configured
    with pytest.raises(cf.StagingError): store.handle(actor,request(configured,**changes))
    assert not store.session_database.exists()


def test_failed_backend_is_durable_and_replay_never_duplicates(configured):
    store,actor,_,_=configured
    result=store.handle(actor,request(configured))
    class Failed:
        def create_or_resume(self,_): raise RuntimeError("disposable backend failure")
    assert launch.start(store,actor,result,backend=Failed())["status"]=="failed"
    backend=Backend()
    assert not launch.start(store,actor,result,backend=backend)["ok"]
    assert not backend.records


def test_refresh_preserves_authorized_original_and_gate_is_one_use(configured,monkeypatch):
    store,actor,_,_=configured
    obj=upload((store,actor),b"original")
    result=store.handle(actor,request(configured,attachments=[obj["reference"]]))
    launch.start(store,actor,result,backend=Backend())
    upload((store,actor),b"newer",previous=obj["reference"])
    class Executed(Exception): pass
    captured=[]
    def execve(executable,argv,env): captured.append((executable,argv,env)); raise Executed()
    monkeypatch.setattr(os,"execve",execve)
    args=argparse.Namespace(state=str(store.root.parent),auth_dir=str(store.auth_directory),db=str(store.session_database),launch=result["launch_id"],tab=result["tabs"][0])
    with pytest.raises(Executed): launch.execute(args)
    argv=captured[0][1]
    assert argv == launch._native_shell_argv()
    assert Path(captured[0][2]["PWD"]).resolve(strict=True) == Path(configured[2]).resolve(strict=True)
    assert path((store,actor),obj).read_bytes()==b"original"
    with pytest.raises(cf.StagingError,match="consumed"): launch.execute(args)


def test_foreign_revoked_and_stale_attachments_never_launch(configured):
    store,actor,_,_=configured
    obj=upload((store,actor))
    for ref in [dict(obj["reference"],machine_id="mach_"+"b"*32),dict(obj["reference"],account_id="acc_"+"b"*64),dict(obj["reference"],digest="b"*64)]:
        with pytest.raises(cf.StagingError): store.handle(actor,request(configured,attachments=[ref]))
    result=store.handle(actor,request(configured,attachments=[obj["reference"]]))
    import sqlite3
    with sqlite3.connect(store.auth_directory/"termdeck-machines-v1.sqlite3") as db: db.execute("UPDATE machines SET revoked_at_ms=1")
    backend=Backend()
    with pytest.raises(cf.StagingError): launch.start(store,actor,result,backend=backend)
    assert not backend.records


@pytest.mark.parametrize("selection", ["workspace", "root", "symlink"])
def test_real_native_shell_has_host_identity_environment_and_filesystem(configured, selection):
    store, actor, workspace, config = configured
    home = workspace.with_name("disposable-home")
    home.mkdir(mode=0o700)
    helper_bin = home / "normal-user-bin"
    helper_bin.mkdir()
    (home / ".bashrc").write_text(
        'export TERMDECK_FIXTURE_RC="ordinary user startup"\n'
        + "export PATH=" + shlex.quote(str(helper_bin)) + ':"$PATH"\n')
    outside = home / "outside-cwd"
    outside.write_text("owned host file")
    outside.chmod(0o600)
    (workspace / ".beads").mkdir()
    (workspace / ".beads" / "sentinel").write_text("host-visible")
    chosen = workspace
    if selection == "root":
        chosen = Path("/")
    elif selection == "symlink":
        chosen = workspace.with_name("workspace-alias")
        chosen.symlink_to(workspace, target_is_directory=True)
        before = workspace.stat()
        assert before.st_uid == os.geteuid() and before.st_mode & 0o777 == 0o700
        # This is a disposable pytest-owned directory used to exercise a hostile mode through a symlink.
        workspace.chmod(0o777)  # ubs:ignore[py.security.file-permissions] -- the exact owned fixture is intentionally made world-writable
        after = workspace.stat()
        assert after.st_dev == before.st_dev and after.st_ino == before.st_ino
        assert after.st_uid == os.geteuid() and after.st_mode & 0o777 == 0o777
    req = request(configured)
    req.pop("workspace_id")
    req["workspace_path"] = str(chosen)
    receipt = store.handle(actor, req)
    backend = Backend()
    assert launch.start(store, actor, receipt, backend=backend)["status"] == "started"
    proof = home / "proof.json"
    probe = home / "probe.py"
    probe.write_text("""import json,os,pathlib,sys
outside,proof,workspace=map(pathlib.Path,sys.argv[1:])
assert outside.read_text()=='owned host file'
outside.write_text('native write outside selected cwd')
assert (workspace/'.beads/sentinel').read_text()=='host-visible'
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
proof.write_text(json.dumps(dict(cwd=os.getcwd(),uid=os.getuid(),euid=os.geteuid(),gid=os.getgid(),egid=os.getegid(),groups=os.getgroups(),home=os.environ['HOME'],path=os.environ['PATH'],marker=os.environ.get('TERMDECK_FIXTURE_RC'),inherited=os.environ.get('TERMDECK_FIXTURE_ENV'),nnp=status['NoNewPrivs'].strip(),mount=os.readlink('/proc/self/ns/mnt'))))
""")
    environment = {**os.environ, "HOME": str(home), "SHELL": "/bin/bash",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                   "TERMDECK_FIXTURE_ENV": "preserved caller environment"}
    command = shlex.split(backend.records[0].command)[1:]
    typed = shlex.join([sys.executable, str(probe), str(outside), str(proof), str(workspace)]) + "\nexit\n"
    completed = subprocess.run(command, input=typed, env=environment, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    observed = _fixture_object(proof, context="host-shell proof")
    assert observed["cwd"] == str(chosen.resolve())
    assert [observed[key] for key in ("uid", "euid", "gid", "egid")] == [os.getuid(), os.geteuid(), os.getgid(), os.getegid()]
    assert observed["groups"] == os.getgroups()
    assert observed["mount"] == os.readlink("/proc/self/ns/mnt")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert observed["nnp"] == status["NoNewPrivs"].strip()
    assert observed["home"] == str(home)
    assert observed["path"].split(":")[0] == str(helper_bin)
    assert observed["marker"] == "ordinary user startup"
    assert observed["inherited"] == "preserved caller environment"
    assert outside.read_text() == "native write outside selected cwd"


def test_configured_entrypoint_cannot_bypass_authority_via_shell_fallback(configured):
    store,actor,_,_=configured
    result=store.handle(actor,request(configured))
    backend=Backend(); launch.start(store,actor,result,backend=backend)
    spec=TmuxBackend()._launch_spec(backend.records[0])
    assert "application exited; interactive shell ready" not in spec.command
    assert "termdeck.agent_launch" in spec.command


def test_native_codex_adapter_passes_only_server_paths_and_private_home(configured,monkeypatch):
    store,actor,workspace,config=configured
    private=store.root.parent/"private-codex"; private.mkdir(mode=0o700)
    source=store.root.parent/"source-codex"; source.mkdir(mode=0o700)
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher",lambda *_a,**_k:None)
    from termdeck.codex_config_scope import prepare_shared_codex_configuration
    prepare_shared_codex_configuration(private,source)
    (private/"auth.json").write_text('{"fixture":true}')
    executable=store.root.parent/"codex-fixture"
    executable.write_text("#!/usr/bin/python\nimport json,os,pathlib,sys\npathlib.Path('codex-proof').write_text(json.dumps({'argv':sys.argv[1:],'home':os.environ.get('CODEX_HOME'),'auth':json.loads(pathlib.Path(os.environ['CODEX_HOME'],'auth.json').read_text())}))\n")
    executable.chmod(0o700)
    import hashlib
    config["codex"]={"executable":str(executable),"sha256":hashlib.sha256(executable.read_bytes()).hexdigest(),"account_source":str(source)}
    (store.root.parent/launch.POLICY).write_text(json.dumps(config))
    obj=upload((store,actor),display_path="/client/never-server/foo")
    result=store.handle(actor,request(configured,runtime="codex",task="Review files; $(not a shell command)",attachments=[obj["reference"]]))
    launch.start(store,actor,result,backend=Backend())
    monkeypatch.setattr(SessionStore,"ensure_auth_scope",lambda self,identity,**_kwargs:replace(self.get(identity),codex_home=str(private),auth_scope_id="auth-fixture"))
    class Executed(Exception): pass
    captured=[]
    completed=[]
    def execve(_descriptor,argv,environment):
        info=os.fstat(_descriptor)
        pinned=Path(os.readlink(f"/proc/self/fd/{_descriptor}")).resolve(strict=True)
        assert pinned==executable.resolve(strict=True)
        assert info.st_uid in {0,os.geteuid()} and info.st_mode & 0o111 and not info.st_mode & 0o022
        assert hashlib.sha256(os.pread(_descriptor,info.st_size,0)).hexdigest()==config["codex"]["sha256"]
        captured.append(argv)
        completed.append(subprocess.run(argv,env=environment,capture_output=True,text=True,timeout=10))
        raise Executed()
    monkeypatch.setattr(os,"execve",execve)
    args=argparse.Namespace(state=str(store.root.parent),auth_dir=str(store.auth_directory),db=str(store.session_database),launch=result["launch_id"],tab=result["tabs"][0])
    with pytest.raises(Executed): launch.execute(args)
    assert completed[0].returncode==0,completed[0].stderr
    proof=_fixture_object(workspace/"codex-proof", context="native Codex proof")
    assert proof["home"]==str(private) and proof["auth"]=={"fixture":True}
    assert "/client/never-server/foo" not in " ".join(proof["argv"])
    assert str(path((store,actor),obj).parent.parent) in proof["argv"][-1]
    assert "$(not a shell command)" in proof["argv"][-1]
    assert proof["argv"][:7] == ["-c", f"sqlite_home={json.dumps(str(private))}",
                                  "-c", f"log_dir={json.dumps(str(private / 'log'))}",
                                  "--no-daemon", "--no-alt-screen", "--"]
    assert not {"--sandbox", "--ask-for-approval", "--dangerously-bypass-approvals-and-sandbox"}.intersection(proof["argv"])
    assert len(proof["argv"]) == 8


def test_concurrent_refresh_and_launch_either_binds_original_or_rejects_stale(configured):
    store,actor,_,_=configured
    obj=upload((store,actor))
    req=request(configured,attachments=[obj["reference"]])
    def prepare():
        try: return store.handle(actor,req)
        except cf.StagingError as error:
            assert error.code=="stale-reference"
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(prepare)
        b=executor.submit(upload,(store,actor),b"changed",previous=obj["reference"])
        result=a.result(); b.result()
    if result:
        restarted=cf.StagingStore(store.root.parent,store.auth_directory)
        assert launch.start(restarted,actor,result,backend=Backend())["status"]=="started"
        assert path((store,actor),obj).read_bytes()==b"hello"


def test_changed_policy_and_runtime_hash_fail_before_start(configured):
    store,actor,_,config=configured
    result=store.handle(actor,request(configured))
    config["workspaces"][0]["label"]="changed policy"
    (store.root.parent/launch.POLICY).write_text(json.dumps(config))
    backend=Backend()
    with pytest.raises(cf.StagingError,match="changed"): launch.start(store,actor,result,backend=backend)
    assert not backend.records
    config["codex"]={"executable":"/usr/bin/true","sha256":"a"*64,"account_source":str(store.auth_directory)}
    (store.root.parent/launch.POLICY).write_text(json.dumps(config))
    with pytest.raises(cf.StagingError,match="identity changed"): launch.policy(store.root.parent)


@pytest.mark.parametrize("relative", [".termdeck", ".termdeck/tls", ".beads", ".beads/archive"])
def test_policy_accepts_host_accessible_storage_names(configured, relative):
    store, _, workspace, config = configured
    protected = workspace / relative
    protected.mkdir(parents=True, mode=0o700)
    config["workspaces"][0]["path"] = str(protected)
    (store.root.parent / launch.POLICY).write_text(json.dumps(config))
    assert launch.policy(store.root.parent)[0]["workspaces"][0]["path"] == str(protected)
    assert not store.session_database.exists()


@pytest.mark.parametrize("descendant", [False, True])
def test_prepare_accepts_host_accessible_auth_storage(configured, descendant):
    store, actor, workspace, config = configured
    auth = workspace.with_name(workspace.name + "-auth")
    shutil.copytree(store.auth_directory, auth)
    store.auth_directory = auth
    selected = auth / "child" if descendant else auth
    if descendant:
        selected.mkdir(mode=0o700)
    config["workspaces"][0]["path"] = str(selected)
    (store.root.parent / launch.POLICY).write_text(json.dumps(config))
    req = request(configured)
    assert store.handle(actor, req)["status"] == "prepared"
    assert not store.session_database.exists()


def _codex_policy(configured, source):
    store, _, _, config = configured
    executable = store.root.parent / "synthetic-runtime"
    executable.write_text("#!/usr/bin/python\nraise SystemExit(0)\n")
    executable.chmod(0o700)
    config["codex"] = {
        "executable": str(executable),
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "account_source": str(source),
    }
    (store.root.parent / launch.POLICY).write_text(json.dumps(config))


def test_yolo_launch_receipt_is_replay_safe_and_keeps_directory(configured):
    store, actor, workspace, _ = configured
    source = store.root.parent / "account-catalogue"
    source.mkdir(mode=0o700)
    _codex_policy(configured, source)
    child = workspace / "selected child"
    child.mkdir()
    req = request(configured, runtime="codex", workspace_id=None,
                  workspace_path=str(child), tabs=["selected child"],
                  task="Inspect this directory", yolo=True)
    req.pop("workspace_id")
    receipt = store.handle(actor, req)
    assert store.handle(actor, req)["launch_id"] == receipt["launch_id"]
    backend = Backend()
    assert launch.start(store, actor, receipt, backend=backend)["status"] == "started"
    assert launch.start(store, actor, receipt, backend=backend)["status"] == "started"
    assert len(backend.records) == 1
    assert backend.records[0].cwd == str(child)
    tree = cf.Directory(store.root)
    try:
        with cf._database(tree, "metadata.sqlite3", readonly=True) as db:
            plan = _fixture_object(db.execute("SELECT plan FROM launches WHERE launch_id=?", (receipt["launch_id"],)).fetchone()[0], context="stored launch plan")
    finally:
        tree.close()
    assert plan["yolo"] is True
    assert "--yolo" in launch._runtime_argv(launch.policy(store.root.parent)[0], plan, "/private")


@pytest.mark.parametrize("descendant", [False, True])
def test_policy_accepts_host_accessible_account_catalogue(configured, descendant):
    store, _, workspace, config = configured
    source = workspace.with_name(workspace.name + "-catalogue")
    source.mkdir(mode=0o700)
    selected = source / "accounts" if descendant else source
    if descendant:
        selected.mkdir(mode=0o700)
    config["workspaces"][0]["path"] = str(selected)
    _codex_policy(configured, source)
    assert launch.policy(store.root.parent)[0]["workspaces"][0]["path"] == str(selected)
    assert not store.session_database.exists()


@pytest.mark.parametrize("target_name", [
    "catalogue", "accounts", "auth-file", "web-auth", "current-home",
    "other-home", "runtime", "runtime-parent", "ledger", "tls",
    "nested-ledger",
])
def test_shared_asset_alias_cannot_broaden_launch(configured, monkeypatch, target_name):
    store, actor, workspace, _ = configured
    source = store.root.parent / "catalogue"
    source.mkdir(mode=0o700)
    private = store.root.parent / "terminal-auth/current"
    private.mkdir(parents=True, mode=0o700)
    targets = {
        "catalogue": source,
        "accounts": source / "accounts",
        "auth-file": source / "auth.json",
        "web-auth": store.auth_directory,
        "current-home": private,
        "other-home": private.with_name("other"),
        "runtime": store.root.parent,
        "runtime-parent": store.root.parent.parent,
        "ledger": workspace / ".beads",
        "tls": workspace / ".termdeck/tls",
        "nested-ledger": source / "rules/.beads",
    }
    target = targets[target_name]
    if target_name == "auth-file":
        target.write_text("synthetic fixture only")
        target.chmod(0o600)
    elif not target.exists():
        target.mkdir(parents=True, mode=0o700)
    (source / "skills").symlink_to(target, target_is_directory=target.is_dir())
    (private / "skills").symlink_to(source / "skills", target_is_directory=True)
    _codex_policy(configured, source)
    result = store.handle(actor, request(configured, runtime="codex"))
    launch.start(store, actor, result, backend=Backend())
    monkeypatch.setattr(SessionStore, "ensure_auth_scope", lambda self, identity, **_kwargs: replace(
        self.get(identity), codex_home=str(private), auth_scope_id="auth-fixture",
    ))
    def unexpected_exec(*_args):
        pytest.fail("A protected shared alias reached runtime execution")
    monkeypatch.setattr(os, "execve", unexpected_exec)
    args = argparse.Namespace(
        state=str(store.root.parent), auth_dir=str(store.auth_directory),
        db=str(store.session_database), launch=result["launch_id"], tab=result["tabs"][0],
    )
    with pytest.raises(cf.StagingError, match="Shared asset"):
        launch.execute(args)


def test_prepared_asset_aliases_resolve_without_rewriting_host_links(configured):
    store, _, workspace, _ = configured
    source = store.root.parent / "catalogue"
    source.mkdir(mode=0o700)
    private = store.root.parent / "terminal-auth/current"
    private.mkdir(parents=True, mode=0o700)
    external = workspace.with_name(workspace.name + "-shared-skills")
    external.mkdir(mode=0o700)
    (external / "SKILL.md").write_text("approved skill")
    (source / "skills").symlink_to(external, target_is_directory=True)
    for name in ("config.toml", "extra.config.toml"):
        (source / name).write_text("fixture = true\n")
        (source / name).chmod(0o600)
    for name in ("skills", "config.toml", "extra.config.toml"):
        (private / name).symlink_to(source / name)
    for name in ("auth.json", ".env", "unselected"):
        (source / name).write_text("must remain outside")
        (source / name).chmod(0o600)
    (external / "unapproved-link").symlink_to(source / "auth.json")
    (private / ".env").symlink_to(source / ".env")
    links_before = {p.name: os.readlink(p) for p in private.iterdir()}
    launch._validate_shared_assets(
        private, state_root=store.root.parent, auth_directory=store.auth_directory,
        account_source=source, workspace=workspace,
    )
    assert (private / "skills/SKILL.md").read_text() == "approved skill"
    # Host-user access follows ordinary links; no mount mask is promised.
    assert (private / "skills/unapproved-link").read_text() == "must remain outside"
    for name in ("config.toml", "extra.config.toml"):
        assert (private / name).read_text() == "fixture = true\n"
    assert {p.name: os.readlink(p) for p in private.iterdir()} == links_before
    assert (source / "extra.config.toml").read_text() == "fixture = true\n"


def test_native_private_config_write_preserves_canonical_defaults(configured, monkeypatch):
    from termdeck.codex_config_scope import prepare_shared_codex_configuration
    from termdeck.native_config_view import is_managed_private_config
    store, _, workspace, _ = configured
    source = store.root.parent / "catalogue"
    source.mkdir(mode=0o700)
    for name in ("config.toml", "extra.config.toml"):
        (source / name).write_text("fixture = true\n")
        (source / name).chmod(0o600)
    (source / "auth.json").write_text("unselected synthetic credential")
    private = store.root.parent / "terminal-auth/current"
    private.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher", lambda *_a, **_k: None)
    prepare_shared_codex_configuration(private, source)
    arguments = dict(state_root=store.root.parent, auth_directory=store.auth_directory,
                     account_source=source, workspace=workspace)
    launch._validate_shared_assets(private, **arguments)
    assert launch._prepare_native_configuration(private, **arguments) == "initialized"
    launch._validate_shared_assets(private, **arguments)
    before = (source / "config.toml").read_bytes()
    code = '''import pathlib,sys
private,source = map(pathlib.Path,sys.argv[1:])
assert (source/'auth.json').read_text() == 'unselected synthetic credential'
config=private/'config.toml'
assert not config.is_symlink()
temporary=private/'native-trust-write.tmp'
temporary.write_bytes(config.read_bytes()+b'\\n[projects."/disposable"]\\ntrust_level="trusted"\\n')
temporary.chmod(0o600)
temporary.replace(config)
'''
    completed = subprocess.run([sys.executable, "-c", code, str(private), str(source)],
                               cwd=workspace, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert (source / "config.toml").read_bytes() == before
    assert "trust_level" in (private / "config.toml").read_text()
    assert is_managed_private_config(private, source)
    assert prepare_shared_codex_configuration(private, source).configuration_ready
    assert launch._prepare_native_configuration(private, **arguments) == "ready"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("target", ["source-auth", "other-home", "web-auth", "runtime"])
def test_config_alias_is_protected_before_read_even_with_private_regular_view(configured, monkeypatch, managed, target):
    from termdeck.codex_config_scope import prepare_shared_codex_configuration
    from termdeck import native_config_view
    store, _, workspace, _ = configured
    source = store.root.parent / "catalogue"
    source.mkdir(mode=0o700)
    config = source / "config.toml"
    config.write_text("fixture = true\n")
    config.chmod(0o600)
    private = store.root.parent / "terminal-auth/current"
    private.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher", lambda *_a, **_k: None)
    prepare_shared_codex_configuration(private, source)
    arguments = dict(state_root=store.root.parent, auth_directory=store.auth_directory,
                     account_source=source, workspace=workspace)
    if managed:
        launch._prepare_native_configuration(private, **arguments)
    before = (private / "config.toml").read_bytes()
    protected = {"source-auth": source / "auth.json", "other-home": private.with_name("other") / "config.toml",
                 "web-auth": store.auth_directory / "config.toml", "runtime": store.root.parent / "config.toml"}[target]
    protected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    protected.write_text('token = "synthetic-must-not-be-copied"\n')
    protected.chmod(0o600)
    config.unlink()
    config.symlink_to(protected)
    def unexpected_read(*_args):
        pytest.fail("A protected config target reached the defaults reader")
    monkeypatch.setattr(native_config_view, "_source_bytes", unexpected_read)
    for action in (lambda: launch._prepare_native_configuration(private, **arguments),
                   lambda: launch._validate_shared_assets(private, **arguments)):
        with pytest.raises(cf.StagingError, match="Shared asset"):
            action()
    if managed:
        assert (private / "config.toml").read_bytes() == before
    else:
        assert (private / "config.toml").is_symlink()
    assert protected.read_text() == 'token = "synthetic-must-not-be-copied"\n'


@pytest.mark.parametrize("unsafe", ["symlink", "public-mode", "file"])
def test_private_log_refuses_unsafe_existing_destination(tmp_path, unsafe):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if unsafe == "symlink":
        (private / "log").symlink_to(outside, target_is_directory=True)
    elif unsafe == "file":
        (private / "log").write_text("preserve")
    else:
        (private / "log").mkdir(mode=0o755)
        (private / "log").chmod(0o755)
    with pytest.raises(cf.StagingError, match="log directory"):
        launch._prepare_private_log_directory(private)
    assert not list(outside.iterdir())


def test_private_log_is_idempotent_and_native_paths_stay_private(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    launch._prepare_private_log_directory(private)
    identity = (private / "log").stat().st_ino
    launch._prepare_private_log_directory(private)
    assert (private / "log").stat().st_ino == identity
    assert (private / "log").stat().st_mode & 0o777 == 0o700
    argv = launch._runtime_argv({"codex": {"executable": "/usr/bin/codex"}},
                               {"task": "task", "attachments": []}, str(private))
    assert "sqlite_home=" + json.dumps(str(private)) in argv
    assert "log_dir=" + json.dumps(str(private / "log")) in argv
    assert argv.count("--no-daemon") == 1
    assert argv[-3] == "--no-alt-screen"


def test_yolo_is_a_separate_codex_argument_and_survives_resume(tmp_path):
    plan = {"task": "check $(touch /tmp/never-execute)", "attachments": [], "yolo": True}
    value = {"codex": {"executable": "/usr/bin/codex"}}
    argv = launch._runtime_argv(value, plan, str(tmp_path))
    assert argv.count("--no-alt-screen") == 1
    assert argv.count("--no-daemon") == 1
    assert argv[-3] == "--yolo"
    assert argv[-2] == "--"
    assert argv[-1].startswith(plan["task"])
    resumed = launch._runtime_argv(value, plan, str(tmp_path), "conversation")
    assert resumed.count("--no-alt-screen") == 1
    assert resumed.count("--no-daemon") == 1
    assert resumed[-3:] == [
        "--yolo", "resume", "conversation",
    ]


@pytest.mark.parametrize("mode", ["true", 1, [], None])
def test_malformed_yolo_mode_is_rejected(configured, mode):
    store, actor, _, _ = configured
    with pytest.raises(cf.StagingError, match="Invalid Codex launch mode"):
        store.handle(actor, request(configured, yolo=mode))


def test_shell_cannot_request_yolo(configured):
    store, actor, _, _ = configured
    with pytest.raises(cf.StagingError, match="Invalid Codex launch mode"):
        store.handle(actor, request(configured, yolo=True))


@pytest.mark.parametrize("masked_source", [False, True])
def test_safe_shared_assets_keep_selected_binding_without_host_isolation(configured, masked_source):
    store, _, workspace, _ = configured
    source = workspace / ".termdeck/catalogue" if masked_source else store.root.parent / "catalogue"
    source.mkdir(parents=True, mode=0o700)
    (source / "config.toml").write_text("fixture = true\n")
    (source / "config.toml").chmod(0o600)
    (source / "unselected").write_text("unselected fixture")
    selected = source / "accounts/selected.json"
    selected.parent.mkdir(mode=0o700)
    selected.write_text("selected fixture")
    selected.chmod(0o600)
    private = store.root.parent / "terminal-auth/current"
    private.mkdir(parents=True, mode=0o700)
    private.with_name("other").mkdir(mode=0o700)
    (private.with_name("other") / "sentinel").write_text("other private fixture")
    (private / "config.toml").symlink_to(source / "config.toml")
    (private / "auth.json").symlink_to(selected)
    external = workspace.with_name(workspace.name + "-skills")
    external.mkdir(mode=0o700)
    (external / "SKILL.md").write_text("shared fixture")
    (private / "skills").symlink_to(external, target_is_directory=True)
    _codex_policy(configured, source)
    assert launch.options(store.root.parent)["workspaces"][0]["path"] == str(workspace)
    assets = [launch._shared_asset_path(
        private / name, state_root=store.root.parent, auth_directory=store.auth_directory,
        account_source=source, private_home=private, workspace=workspace,
        shared_names={"config.toml", "skills"},
    ) for name in ("config.toml", "skills")]
    code = '''import pathlib,sys
private,source = map(pathlib.Path,sys.argv[1:])
assert (private/'config.toml').read_text() == 'fixture = true\\n'
assert (private/'skills/SKILL.md').read_text() == 'shared fixture'
assert (private/'auth.json').read_text() == 'selected fixture'
assert (source/'unselected').read_text() == 'unselected fixture'
assert (private.parent/'other/sentinel').read_text() == 'other private fixture'
'''
    assert assets == [str((private / name).resolve()) for name in ("config.toml", "skills")]
    completed = subprocess.run([sys.executable, "-c", code, str(private), str(source)],
                               cwd=workspace, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_cached_confined_options_require_refresh(configured):
    store, actor, _, _ = configured
    stale_revision = hashlib.sha256((store.root.parent / launch.POLICY).read_bytes()).hexdigest()
    with pytest.raises(cf.StagingError, match="refresh choices"):
        store.handle(actor, request(configured, policy_revision=stale_revision))
    assert not store.session_database.exists()


@pytest.mark.parametrize("phase", ["start", "execute"])
@pytest.mark.parametrize("kind", ["unversioned", "wrong-version", "wrong-execution"])
def test_old_or_unknown_plan_cannot_gain_host_user_authority(configured, monkeypatch, phase, kind):
    store, actor, _, _ = configured
    result = store.handle(actor, request(configured))
    backend = Backend()
    if phase == "execute":
        launch.start(store, actor, result, backend=backend)
    with sqlite3.connect(store.root / "metadata.sqlite3") as db:
        row = db.execute("SELECT plan FROM launches WHERE launch_id=?", (result["launch_id"],)).fetchone()
        plan = _fixture_object(row[0], context="legacy-plan fixture")
        assert plan["version"] == 2 and plan["execution"] == "host-user"
        if kind == "unversioned":
            plan.pop("version")
            plan.pop("execution")
        elif kind == "wrong-version":
            plan["version"] = 1
        else:
            plan["execution"] = "confined"
        db.execute("UPDATE launches SET plan=? WHERE launch_id=?", (json.dumps(plan), result["launch_id"]))
    def unexpected(*_args):
        pytest.fail("Legacy intent reached a host-user process")
    monkeypatch.setattr(os, "execve", unexpected)
    with pytest.raises(cf.StagingError, match="new authenticated host-user launch"):
        if phase == "start":
            launch.start(store, actor, result, backend=backend)
        else:
            launch.execute(argparse.Namespace(state=str(store.root.parent), auth_dir=str(store.auth_directory),
                           db=str(store.session_database), launch=result["launch_id"], tab=result["tabs"][0]))
    if phase == "start":
        assert not backend.records and not store.session_database.exists()
    with sqlite3.connect(store.root / "metadata.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='launch_runs'").fetchone()


def test_native_shell_uses_passwd_when_shell_is_unset_or_unusable(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(launch.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_shell="/bin/bash"))
    for shell in (None, "/nonexistent/native-shell", "/bin/bash -c injected"):
        if shell is None:
            monkeypatch.delenv("SHELL", raising=False)
        else:
            monkeypatch.setenv("SHELL", shell)
        assert launch._native_shell_argv() == [str(Path("/bin/bash").resolve()), "-i"]


def test_assigned_private_bash_scope_survives_normal_user_startup(configured, monkeypatch):
    store, actor, workspace, _ = configured
    home = workspace.with_name("shell-home")
    home.mkdir(mode=0o700)
    (home / ".bashrc").write_text('export CODEX_HOME="/foreign-user-config"\nexport PATH="/usr/bin:/bin"\nexport NORMAL_RC_LOADED=yes\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher", lambda *_a, **_k: None)
    source = store.root.parent / "synthetic-source"
    source.mkdir(mode=0o700)
    receipt = store.handle(actor, request(configured))
    backend = Backend()
    launch.start(store, actor, receipt, backend=backend)
    sessions = SessionStore(store.session_database, account_source=source, provision_auth_scopes=False)
    try:
        record = sessions.ensure_auth_scope(receipt["tabs"][0], private_history=True)
    finally:
        sessions.close()
    private, scope = Path(record.codex_home), record.auth_scope_id
    proof = workspace / "scoped-shell.json"
    program = "import json,os,pathlib; pathlib.Path('scoped-shell.json').write_text(json.dumps({key:os.environ.get(key) for key in ('CODEX_HOME','TERMDECK_AUTH_SCOPE_ID','PATH','NORMAL_RC_LOADED','HOME')}))"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    completed = subprocess.run(shlex.split(backend.records[0].command)[1:],
                               input=shlex.join([sys.executable, "-c", program])+"\nexit\n",
                               env=environment, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    observed = _fixture_object(proof, context="scoped-shell proof")
    assert observed["CODEX_HOME"] == str(private)
    assert observed["TERMDECK_AUTH_SCOPE_ID"] == scope
    assert observed["PATH"] == str(private / "bin") + ":/usr/bin:/bin"
    assert observed["NORMAL_RC_LOADED"] == "yes" and observed["HOME"] == str(home)


def test_profile_admission_is_readonly_and_works_inside_initial_authorization(configured):
    store, actor, _, _ = configured
    obj = upload((store, actor), b"original")
    receipt = store.handle(actor, request(configured, attachments=[obj["reference"]]))
    class AdmittedBackend(Backend):
        def create_or_resume(self, record):
            launch.validate_native_launch(record)
            super().create_or_resume(record)
    backend = AdmittedBackend()
    assert launch.start(store, actor, receipt, backend=backend)["status"] == "started"
    files = [store.root / "metadata.sqlite3", store.session_database,
             store.auth_directory / "termdeck-machines-v1.sqlite3",
             store.auth_directory / "termdeck-web-auth-v1.sqlite3"]
    before = {file: (file.stat().st_ino, file.read_bytes()) for file in files}
    launch.validate_native_launch(backend.records[0])
    launch.validate_native_launch(backend.records[0])
    assert {file: (file.stat().st_ino, file.read_bytes()) for file in files} == before
    with sqlite3.connect(store.root / "metadata.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='launch_runs'").fetchone()


@pytest.mark.parametrize("mutation,detail", [
    ("legacy", "new authenticated host-user launch"), ("machine", "authorization failed"),
    ("expired-login", "authorization failed"), ("policy", "policy changed"),
    ("attachment", "no longer authorized"), ("cwd", "directory identity changed"),
])
def test_profile_admission_refuses_invalid_authority(configured, mutation, detail):
    store, actor, workspace, config = configured
    obj = upload((store, actor), b"original")
    receipt = store.handle(actor, request(configured, attachments=[obj["reference"]]))
    backend = Backend()
    launch.start(store, actor, receipt, backend=backend)
    if mutation == "legacy":
        with sqlite3.connect(store.root / "metadata.sqlite3") as db:
            plan = _fixture_object(db.execute("SELECT plan FROM launches WHERE launch_id=?", (receipt["launch_id"],)).fetchone()[0], context="stored launch plan")
            plan.pop("version")
            db.execute("UPDATE launches SET plan=? WHERE launch_id=?", (json.dumps(plan), receipt["launch_id"]))
    elif mutation == "policy":
        config["workspaces"][0]["label"] = "changed"
        (store.root.parent / launch.POLICY).write_text(json.dumps(config))
    elif mutation == "cwd":
        workspace.rename(workspace.with_name("original"))
        workspace.mkdir()
    else:
        database, statement = {
            "machine": (store.auth_directory / "termdeck-machines-v1.sqlite3", "UPDATE machines SET revoked_at_ms=1"),
            "expired-login": (store.auth_directory / "termdeck-web-auth-v1.sqlite3", "UPDATE auth_sessions SET expires_at_ms=1"),
            "attachment": (store.root / "metadata.sqlite3", "UPDATE attachments SET target='window:other'"),
        }[mutation]
        with sqlite3.connect(database) as db:
            db.execute(statement)
    with pytest.raises(cf.StagingError, match=detail):
        launch.validate_native_launch(backend.records[0])


def test_profile_admission_does_not_recreate_missing_staging_storage(configured):
    store, actor, _, _ = configured
    receipt = store.handle(actor, request(configured))
    backend = Backend()
    launch.start(store, actor, receipt, backend=backend)
    moved = store.root.with_name("preserved-client-files")
    store.root.rename(moved)
    with pytest.raises((cf.StagingError, OSError)):
        launch.validate_native_launch(backend.records[0])
    assert not store.root.exists()
    assert moved.is_dir()


def test_nested_profile_admission_keeps_machine_revocation_serialized(configured):
    store, actor, _, _ = configured
    receipt = store.handle(actor, request(configured))
    backend = Backend()
    launch.start(store, actor, receipt, backend=backend)
    program = """import sqlite3,sys
connection=sqlite3.connect(sys.argv[1],timeout=0)
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error)
    sys.exit(0)
else:
    connection.rollback()
    sys.exit(1)
"""
    def competing_writer_is_blocked():
        process = subprocess.run([sys.executable, "-c", program, str(store.auth_directory / "termdeck-machines-v1.sqlite3")],
                                 capture_output=True, text=True, timeout=5)
        assert process.returncode in {0, 1}, process.stderr
        return process.returncode == 0
    with cf.authorized(store.auth_directory, actor):
        assert competing_writer_is_blocked()
        launch.validate_native_launch(backend.records[0])
        assert competing_writer_is_blocked(), "Read-only admission released the outer SQLite revocation lock"
        with pytest.raises(cf.StagingError, match="does not match"):
            with cf.authorized(store.auth_directory, replace(actor, session_id="ses_" + "f" * 32), readonly=True):
                pytest.fail("Different actor reused the original authorization")
        assert competing_writer_is_blocked()
    assert not competing_writer_is_blocked()


def test_readonly_admission_never_reuses_expired_context_or_foreign_directory(configured, tmp_path):
    from contextvars import copy_context
    from test_client_files import make_stage
    store, actor, _, _ = configured
    other_root = tmp_path / "other-state"
    other_root.mkdir(mode=0o700)
    other_store, _ = make_stage(other_root, other_root / "auth", owner="other-owner", suffix="b")
    with cf.authorized(store.auth_directory, actor):
        saved_context = copy_context()
        with pytest.raises(cf.StagingError, match="authorization failed"):
            with cf.authorized(other_store.auth_directory, actor, readonly=True):
                pytest.fail("Different auth directory reused authorization")
    with sqlite3.connect(store.auth_directory / "termdeck-machines-v1.sqlite3") as db:
        db.execute("UPDATE machines SET revoked_at_ms=1")
    def admit():
        with cf.authorized(store.auth_directory, actor, readonly=True):
            pytest.fail("An exited context admitted a revoked actor")
    with pytest.raises(cf.StagingError, match="authorization failed"):
        saved_context.run(admit)


@pytest.fixture
def managed_launch_context(tmp_path, monkeypatch):
    from termdeck import build_info
    from test_build_info import release_build_info, release_manifest
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    launcher = root / "bin" / "t1000"
    launcher.parent.mkdir(mode=0o700)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    (root / "bootstrap.py").write_bytes(b"# fixture bootstrap\n")
    state = {"schema": build_info.MANAGED_STATE_SCHEMA, "generation": 0,
             "current_release_id": None, "previous_release_id": None,
             "manifest_sha256": "", "legacy_launcher": None,
             "python_executable": str(Path(sys.executable).resolve()),
             "bootstrap_sha256": build_info.sha256_file(root / "bootstrap.py"),
             "launcher_path": str(launcher), "launcher_sha256": build_info.sha256_file(launcher)}
    def activate(seed):
        info = release_build_info(seed)
        manifest = release_manifest(info)
        manifest_path = root / "releases" / manifest["release_id"] / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(build_info.canonical_json_bytes(manifest))
        state.update(generation=state["generation"] + 1,
                     previous_release_id=state["current_release_id"],
                     current_release_id=manifest["release_id"],
                     manifest_sha256=build_info.sha256_file(manifest_path))
        (root / "state.json").write_bytes(build_info.canonical_json_bytes(state))
        monkeypatch.setattr(build_info, "load_bundled_build_info", lambda: info)
        monkeypatch.setenv("T1000_MANAGED_ROOT", str(root))
        monkeypatch.setenv("T1000_RELEASE_STATE", str(root / "state.json"))
        monkeypatch.setenv("T1000_RELEASE_MANIFEST", str(manifest_path))
    activate("a")
    return root, launcher, state, activate


def test_managed_command_binds_verified_stable_entrypoint_and_allows_release_upgrade(configured, managed_launch_context, monkeypatch):
    store, actor, workspace, _ = configured
    root, launcher, _, activate = managed_launch_context
    result = store.handle(actor, request(configured, tabs=["One"]))
    backend = Backend()
    assert launch.start(store, actor, result, backend=backend)["status"] == "started"
    record = backend.records[0]
    assert shlex.split(record.command) == ["exec", str(launcher), "agent-launch",
        "--state", str(store.root.parent), "--auth-dir", str(store.auth_directory),
        "--db", str(store.session_database), "--launch", result["launch_id"], "--tab", record.id]
    with sqlite3.connect(store.root / "metadata.sqlite3") as db:
        plan = _fixture_object(db.execute("SELECT plan FROM launches").fetchone()[0], context="managed launch plan")
    assert plan["managed_entrypoint"] == {"root": str(root), "launcher": str(launcher)}
    launch.validate_native_launch(record)
    activate("b")
    launch.validate_native_launch(record)
    captured = []
    class Executed(Exception): pass
    def execve(executable, argv, environment):
        captured.append((executable, argv, environment, os.getcwd()))
        raise Executed()
    monkeypatch.setattr(os, "execve", execve)
    with pytest.raises(Executed):
        launch.execute(launch._launch_binding(record))
    assert captured[0][1] == launch._native_shell_argv()
    assert captured[0][3] == str(workspace)


@pytest.mark.parametrize("shape", ["source", "managed"])
@pytest.mark.parametrize("change", ["extra", "other-module", "wrong-flag", "relative-executable", "external"])
def test_launch_binding_rejects_other_dispatch_shapes(configured, shape, change):
    store, actor, _, _ = configured
    result = store.handle(actor, request(configured, tabs=["One"]))
    backend = Backend()
    launch.start(store, actor, result, backend=backend)
    record = backend.records[0]
    parts = shlex.split(record.command)
    if shape == "managed":
        parts[1:4] = ["/fixture/bin/t1000", "agent-launch"]
    assert launch._launch_binding(replace(record, command=shlex.join(parts))).tab == record.id
    if change == "extra": parts += ["--resume-permit", "resume_" + "a" * 32]
    elif change == "other-module": parts[2] = "other-command"
    elif change == "wrong-flag": parts[-2] = "--command"
    elif change == "relative-executable": parts[1] = "t1000"
    with pytest.raises(cf.StagingError, match="binding is unavailable"):
        launch._launch_binding(replace(record, command=shlex.join(parts), external=change == "external"))


@pytest.mark.parametrize("mutation", ["launcher-bytes", "bootstrap-bytes", "state-bytes", "manifest-bytes",
    "launcher-symlink", "launcher-hardlink", "launcher-owner", "root-writable", "root-alias",
    "context-state", "context-manifest", "launcher-path", "python-path", "missing-context"])
def test_managed_launch_authority_drift_refuses_before_start(configured, managed_launch_context, monkeypatch, mutation):
    from termdeck import build_info
    store, actor, _, _ = configured
    root, launcher, state, _ = managed_launch_context
    result = store.handle(actor, request(configured, tabs=["One"]))
    if mutation.endswith("-bytes"):
        path = {"launcher-bytes": launcher, "bootstrap-bytes": root / "bootstrap.py",
                "state-bytes": root / "state.json", "manifest-bytes": Path(os.environ["T1000_RELEASE_MANIFEST"])}[mutation]
        path.write_bytes(path.read_bytes() + b" \n")
    elif mutation == "launcher-symlink":
        moved = launcher.with_name("original")
        launcher.rename(moved)
        launcher.symlink_to(moved)
    elif mutation == "launcher-hardlink": os.link(launcher, launcher.with_name("extra-link"))
    elif mutation == "launcher-owner":
        original = Path.lstat
        def changed_owner(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == launcher:
                fields = list(info)
                fields[4] = os.geteuid() + 1001
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(Path, "lstat", changed_owner)
    elif mutation == "root-writable": root.chmod(0o770)
    elif mutation == "root-alias":
        alias = root.with_name("alias")
        alias.symlink_to(root)
        monkeypatch.setenv("T1000_MANAGED_ROOT", str(alias))
    elif mutation == "context-state": monkeypatch.setenv("T1000_RELEASE_STATE", str(root / "other-state"))
    elif mutation == "context-manifest": monkeypatch.setenv("T1000_RELEASE_MANIFEST", str(root / "other-manifest"))
    elif mutation == "launcher-path":
        other = launcher.with_name("other-t1000")
        other.write_bytes(launcher.read_bytes())
        other.chmod(0o755)
        state["launcher_path"] = str(other)
        (root / "state.json").write_bytes(build_info.canonical_json_bytes(state))
    elif mutation == "python-path":
        state["python_executable"] = "/missing/python"
        (root / "state.json").write_bytes(build_info.canonical_json_bytes(state))
    else:
        for name in ("T1000_MANAGED_ROOT", "T1000_RELEASE_STATE", "T1000_RELEASE_MANIFEST"):
            monkeypatch.delenv(name)
    backend = Backend()
    with pytest.raises(cf.StagingError, match="launch authority|launcher identity"):
        launch.start(store, actor, result, backend=backend)
    assert not backend.records
    assert not store.session_database.exists()


def test_present_invalid_managed_context_never_falls_back_to_source_launch(configured, monkeypatch):
    store, actor, _, _ = configured
    monkeypatch.setenv("T1000_MANAGED_ROOT", str(store.root / "missing"))
    with pytest.raises(cf.StagingError, match="Managed launch authority"):
        store.handle(actor, request(configured))
    assert not store.session_database.exists()
