import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from termdeck import agent_launch as launch
from termdeck import client_files as cf
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend
from test_client_files import stage, call, upload, path  # noqa: F401


@pytest.fixture
def configured(stage):
    store,actor=stage
    workspace=store.root.parent.with_name(store.root.parent.name+"-workspace")
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
    assert str(path((store,actor),obj).parent.parent) in argv
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


def test_real_mount_namespace_has_only_approved_files_and_readonly_attachments(configured):
    store,actor,workspace,_=configured
    obj=upload((store,actor),b"original")
    server_path=path((store,actor),obj).parent.parent
    sentinel=store.root.parent/"host-secret"; sentinel.write_text("secret")
    (workspace/".termdeck").mkdir(); (workspace/".termdeck/secret").write_text("secret")
    (workspace/".beads").mkdir(); (workspace/".beads/secret").write_text("secret")
    (workspace/"escape").symlink_to(sentinel)
    code='''import pathlib,sys
root=pathlib.Path(sys.argv[1]); attachment=pathlib.Path(sys.argv[2]); outside=pathlib.Path(sys.argv[3])
assert (attachment/'project/file.txt').read_bytes()==b'original'
assert not outside.exists()
assert not (root/'.termdeck/secret').exists()
assert not (root/'.beads/secret').exists()
assert not (root/'escape').exists()
try: (attachment/'project/file.txt').write_text('overwrite')
except OSError: pass
else: raise AssertionError('attachment writable')
(root/'result').write_text('confined')
'''
    argv=launch.sandbox_argv(str(workspace),[str(server_path)],runtime_argv=[sys.executable,"-c",code,str(workspace),str(server_path),str(sentinel)])
    result=subprocess.run(argv,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert (workspace/"result").read_text()=="confined"
    assert sentinel.read_text()=="secret" and path((store,actor),obj).read_bytes()==b"original"


def test_confined_task_has_no_host_shell_fallback(configured):
    store,actor,_,_=configured
    result=store.handle(actor,request(configured))
    backend=Backend(); launch.start(store,actor,result,backend=backend)
    spec=TmuxBackend()._launch_spec(backend.records[0])
    assert "application exited; interactive shell ready" not in spec.command
    assert "termdeck.agent_launch" in spec.command


def test_confined_codex_adapter_passes_only_server_paths_and_private_home(configured,monkeypatch):
    store,actor,workspace,config=configured
    private=store.root.parent/"private-codex"; private.mkdir(mode=0o700)
    (private/"auth.json").write_text('{"fixture":true}')
    executable=store.root.parent/"codex-fixture"
    executable.write_text("#!/usr/bin/python\nimport json,os,pathlib,sys\npathlib.Path('codex-proof').write_text(json.dumps({'argv':sys.argv[1:],'home':os.environ.get('CODEX_HOME'),'auth':json.loads(pathlib.Path(os.environ['CODEX_HOME'],'auth.json').read_text())}))\n")
    executable.chmod(0o700)
    import hashlib
    config["codex"]={"executable":str(executable),"sha256":hashlib.sha256(executable.read_bytes()).hexdigest(),"account_source":str(private)}
    (store.root.parent/launch.POLICY).write_text(json.dumps(config))
    obj=upload((store,actor),display_path="/client/never-server/foo")
    result=store.handle(actor,request(configured,runtime="codex",task="Review files; $(not a shell command)",attachments=[obj["reference"]]))
    launch.start(store,actor,result,backend=Backend())
    monkeypatch.setattr(SessionStore,"ensure_auth_scope",lambda self,identity:replace(self.get(identity),codex_home=str(private),auth_scope_id="auth-fixture"))
    class Executed(Exception): pass
    captured=[]
    def execve(_executable,argv,_env): captured.append(argv); raise Executed()
    monkeypatch.setattr(os,"execve",execve)
    args=argparse.Namespace(state=str(store.root.parent),auth_dir=str(store.auth_directory),db=str(store.session_database),launch=result["launch_id"],tab=result["tabs"][0])
    with pytest.raises(Executed): launch.execute(args)
    completed=subprocess.run(captured[0],capture_output=True,text=True,timeout=10)
    assert completed.returncode==0,completed.stderr
    proof=json.loads((workspace/"codex-proof").read_text())
    assert proof["home"]==str(private) and proof["auth"]=={"fixture":True}
    assert "/client/never-server/foo" not in " ".join(proof["argv"])
    assert str(path((store,actor),obj).parent.parent) in proof["argv"][-1]
    assert "$(not a shell command)" in proof["argv"][-1]
    assert proof["argv"][:2]==["-c",f"sqlite_home={json.dumps(str(private))}"]


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
