"""Disposable staging stores; no production credentials or session state."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
from types import SimpleNamespace
import uuid

import pytest

from termdeck import client_files as cf


@pytest.fixture
def ordinary_workspace_root():
    # Use the runner's private temporary storage when configured. A fixed /tmp
    # bypasses its quota and isolation controls on production hosts.
    with tempfile.TemporaryDirectory(prefix="termdeck-workspace-", dir=os.environ.get("TMPDIR")) as directory:
        yield Path(directory)


def make_stage(state_root, auth, *, owner="owner", suffix="a", machine_id=None, extra_machine=False):
    """Independent authoritative account records, optionally sharing staging."""
    state_root.chmod(0o700)
    auth.mkdir(mode=0o700)
    # Match the actual Rust schemas, including login revocation and expiry.
    source = Path("web/crates/termdeck-web-auth/src/store.rs").read_text()
    schema = source.split('const SCHEMA: &str = r#"', 1)[1].split('"#;', 1)[0]
    login = sqlite3.connect(auth / "termdeck-web-auth-v1.sqlite3")
    login.executescript(schema)
    login.execute("INSERT INTO auth_metadata VALUES(1,1,'host',?,0,0)", (owner,))
    device, session = "dev_" + suffix * 32, "ses_" + suffix * 32
    login.execute("INSERT INTO devices VALUES(?,?,0,NULL)", (device, "Browser"))
    login.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,0,?,NULL,NULL)", (session,b"s"*32,b"c"*32,device,10**15))
    login.commit()
    login.close()
    source = Path("web/crates/termdeck-web-auth/src/machines.rs").read_text()
    schema = source.split('const SCHEMA: &str = "', 1)[1].split('";', 1)[0]
    machines = sqlite3.connect(auth / "termdeck-machines-v1.sqlite3")
    machines.executescript(schema)
    machines.execute("INSERT INTO identity VALUES(1,'host',?,1)", (owner,))
    key_hash = hashlib.sha256(("test machine credential:" + owner).encode()).hexdigest()
    machine_id = machine_id or "mach_" + suffix * 32
    machines.execute("INSERT INTO machines VALUES(?,?,?,0,0,NULL,1)", (machine_id, bytes.fromhex(key_hash), owner))
    if extra_machine:
        machines.execute("INSERT INTO machines VALUES(?,?,?,0,0,NULL,1)", ("mach_" + "b"*32, bytes.fromhex("b"*64), "second machine"))
    machines.commit()
    machines.close()
    for db in auth.iterdir():
        db.chmod(0o600)
    account = "acc_" + hashlib.sha256(b"termdeck/account-namespace/v1\0host\0" + owner.encode()).hexdigest()
    actor = cf.Actor(account, machine_id, session, key_hash, 1)
    return cf.StagingStore(state_root, auth), actor


@pytest.fixture
def stage(tmp_path):
    return make_stage(tmp_path, tmp_path / "auth", extra_machine=True)


@pytest.fixture(params=[False, True], ids=["distinct-machine-ids", "same-machine-id"])
def account_stages(tmp_path, request):
    # Equal public machine IDs in independent registries force the account
    # namespace to provide isolation instead of relying on machine-ID entropy.
    a = make_stage(tmp_path, tmp_path / "auth-a", owner="account-a", suffix="a")
    b = make_stage(tmp_path, tmp_path / "auth-b", owner="account-b", suffix="b",
                   machine_id=a[1].machine_id if request.param else None)
    assert a[0].root == b[0].root
    assert a[0].auth_directory != b[0].auth_directory
    assert a[1].account_id != b[1].account_id
    assert a[1].session_id != b[1].session_id
    assert a[1].credential_hash != b[1].credential_hash
    return a, b


def call(stage, operation, **fields):
    store, actor = stage
    if operation in {"initiate", "finalize", "remove", "attach", "detach", "cleanup"}:
        fields.setdefault("request_id", uuid.uuid4().hex)
    return store.handle(actor, {"operation": operation, **fields}, now_ms=1000)


def upload(stage, contents=b"hello", name="project/file.txt", **extra):
    obj = call(stage, "initiate", files=[{"path": name, "size": len(contents)}], **extra)["object"]
    if contents:
        call(stage,"chunk",reference=obj["reference"],file_index=0,offset=0,data=base64.b64encode(contents).decode())
    return call(stage,"finalize",reference=obj["reference"])["object"]


def path(stage, obj):
    store, _ = stage
    return store.root / store._location(obj["reference"]) / obj["files"][0]["path"]


def test_upload_immutable_refresh_attach_download_remove(stage):
    original = upload(stage, display_path="/home/client/project/file.txt")
    reference = original["reference"]
    assert original["files"][0]["digest"] == hashlib.sha256(b"hello").hexdigest()
    assert path(stage, original).read_bytes() == b"hello"
    assert path(stage, original).stat().st_mode & 0o777 == 0o400
    assert str(stage[0].root) not in json.dumps(original)
    attached = call(stage,"attach",reference=reference,target="task:test")
    changed = upload(stage,b"new",previous=reference)
    assert changed["reference"]["generation"] == 2
    assert call(stage,"inspect",reference=reference)["object"]["status"] == "changed"
    resolved = stage[0].handle(stage[1], {"operation":"resolve","attachment_id":attached["attachment_id"]}, now_ms=1000, internal_resolve=True)
    assert Path(resolved["server_path"]).joinpath("project/file.txt").read_bytes() == b"hello"
    assert base64.b64decode(call(stage,"download",reference=reference,file_index=0,offset=0)["data"]) == b"hello"
    with pytest.raises(cf.StagingError):
        call(stage,"attach",reference=reference,target="task:new")
    with pytest.raises(cf.StagingError):
        call(stage,"remove",reference=reference)
    call(stage,"detach",reference=reference,attachment_id=attached["attachment_id"])
    call(stage,"remove",reference=reference)
    assert not path(stage,original).exists()
    assert path(stage,changed).read_bytes() == b"new"


@pytest.mark.parametrize("name", ["../a","a/../../b","/etc/passwd",r"C:\file",r"a\b","a//b","a/%2e%2e/b","a\x00b","a\nb",".beads/data","a/./b","e\u0301.txt","x.","x "])
def test_reject_paths(stage,name):
    with pytest.raises(cf.StagingError):
        call(stage,"initiate",files=[{"path":name,"size":1}])
    assert call(stage,"list")["objects"] == []


@pytest.mark.parametrize("names", [("x","x"),("A/x","a/y"),("K/x","K/y"),("x","x/y"),("Ａ","A")])
def test_reject_collisions(stage,names):
    with pytest.raises(cf.StagingError):
        call(stage,"initiate",files=[{"path":n,"size":0} for n in names])


def test_replay_partial_chunks_digest_and_expiry(stage):
    request = {"operation":"initiate","request_id":uuid.uuid4().hex,"files":[{"path":"x","size":4,"digest":hashlib.sha256(b"abcd").hexdigest()}]}
    a = stage[0].handle(stage[1],request,now_ms=1000)["object"]
    b = stage[0].handle(stage[1],request,now_ms=1000)["object"]
    assert a == b
    with pytest.raises(cf.StagingError):
        stage[0].handle(stage[1],dict(request,label="different"),now_ms=1000)
    chunk=dict(reference=a["reference"],file_index=0,offset=0,data="YWI=")
    assert call(stage,"chunk",**chunk) == call(stage,"chunk",**chunk)
    with pytest.raises(cf.StagingError):
        call(stage,"chunk",**dict(chunk,data="eHg="))
    with pytest.raises(cf.StagingError):
        call(stage,"finalize",reference=a["reference"])
    call(stage,"chunk",reference=a["reference"],file_index=0,offset=2,data="Y2Q=")
    final_id = uuid.uuid4().hex
    ready = call(stage,"finalize",reference=a["reference"],request_id=final_id)["object"]
    assert call(stage,"finalize",reference=a["reference"],request_id=final_id)["object"] == ready
    call(stage,"remove",reference=ready["reference"])
    assert call(stage,"finalize",reference=a["reference"],request_id=final_id)["object"]["status"] == "removed"
    pending = call(stage,"initiate",files=[{"path":"x","size":1}])["object"]
    cleaned = stage[0].handle(stage[1],{"operation":"cleanup","request_id":uuid.uuid4().hex},now_ms=1001+cf.UPLOAD_TTL_MS)
    assert cleaned["expired_uploads"] == 1
    assert not path(stage,pending).exists()


@pytest.mark.parametrize("mutation", ["other-machine","other-account","wrong-key","rotated","revoked","expired","logout","browser-revoked"])
def test_every_operation_revalidates_authority(stage,mutation):
    store, actor = stage
    obj=upload(stage)
    attached=call(stage,"attach",reference=obj["reference"],target="task:test")
    if mutation == "other-machine":
        actor=replace(actor,machine_id="mach_"+"b"*32,credential_hash="b"*64)
    elif mutation == "other-account": actor=replace(actor,account_id="acc_"+"b"*64)
    elif mutation == "wrong-key": actor=replace(actor,credential_hash="b"*64)
    elif mutation == "rotated": actor=replace(actor,credential_generation=2)
    else:
        filename="termdeck-machines-v1.sqlite3" if mutation == "revoked" else "termdeck-web-auth-v1.sqlite3"
        with sqlite3.connect(store.auth_directory/filename) as db:
            db.execute({"revoked":"UPDATE machines SET revoked_at_ms=1", "expired":"UPDATE auth_sessions SET expires_at_ms=1", "logout":"UPDATE auth_sessions SET revoked_at_ms=1", "browser-revoked":"UPDATE devices SET revoked_at_ms=1"}[mutation])
    operations=[{"operation":"inspect","reference":obj["reference"]}, {"operation":"remove","reference":obj["reference"],"request_id":uuid.uuid4().hex},
                {"operation":"attach","reference":obj["reference"],"target":"task:other","request_id":uuid.uuid4().hex},
                {"operation":"resolve","attachment_id":attached["attachment_id"]},
                {"operation":"download","reference":obj["reference"],"file_index":0,"offset":0}]
    if mutation != "other-machine": operations.append({"operation":"list"})
    for request in operations:
        with pytest.raises(cf.StagingError) as rejected:
            store.handle(actor,request,now_ms=1000,internal_resolve=True)
        assert rejected.value.code == "unauthorized"
    if mutation == "other-machine": assert store.handle(actor,{"operation":"list"},now_ms=1000)["objects"] == []


@pytest.mark.parametrize("operation", ["inspect", "download", "attach", "detach", "remove", "resolve", "chunk", "finalize", "refresh"])
def test_valid_accounts_share_storage_without_foreign_object_authority(account_stages, operation):
    objects, requests, before = [], [], []
    for index, context in enumerate(account_stages):
        if operation in {"chunk", "finalize"}:
            obj = call(context, "initiate", files=[{"path": "shared.txt", "size": 1 if operation == "chunk" else 0}])["object"]
        else:
            obj = upload(context, f"account {index}".encode(), name="shared.txt")
        objects.append(obj)
        reference = obj["reference"]
        request = {"operation": operation, "reference": reference}
        if operation in {"attach", "detach", "remove", "finalize", "refresh"}:
            request["request_id"] = uuid.uuid4().hex
        if operation in {"detach", "resolve"}:
            attached = call(context, "attach", reference=reference, target="task:account-proof")
            request["attachment_id"] = attached["attachment_id"]
            if operation == "resolve":
                request.pop("reference")
        elif operation == "attach":
            request["target"] = "task:account-proof"
        elif operation in {"download", "chunk"}:
            request.update(file_index=0, offset=0)
            if operation == "chunk":
                request["data"] = "eA=="
        elif operation == "refresh":
            request = {"operation": "initiate", "request_id": request["request_id"],
                       "previous": reference, "files": [{"path": "shared.txt", "size": 0}]}
        requests.append(request)
        before.append(call(context, "inspect", reference=reference))

    # Both accounts have valid authority and real rows in the same metadata DB.
    for index, context in enumerate(account_stages):
        assert [entry["reference"] for entry in call(context, "list")["objects"]] == [objects[index]["reference"]]
    for index, (store, actor) in enumerate(account_stages):
        with pytest.raises(cf.StagingError) as rejected:
            store.handle(actor, requests[1-index], now_ms=1000, internal_resolve=True)
        assert rejected.value.code == "unauthorized"
    for index, context in enumerate(account_stages):
        assert call(context, "inspect", reference=objects[index]["reference"]) == before[index]
        assert path(context, objects[index]).read_bytes() == (b"" if operation in {"chunk", "finalize"} else f"account {index}".encode())
        store, actor = context
        result = store.handle(actor, requests[index], now_ms=1000, internal_resolve=True)
        assert result["ok"] is True
        if operation == "download":
            assert base64.b64decode(result["data"]) == f"account {index}".encode()
        elif operation == "resolve":
            assert Path(result["server_path"]).joinpath("shared.txt").read_bytes() == f"account {index}".encode()


def test_valid_account_cleanup_and_replay_are_scoped(account_stages):
    pending = []
    request_id = uuid.uuid4().hex
    for context in account_stages:
        obj = call(context, "initiate", request_id=request_id, files=[{"path": "pending", "size": 1}])["object"]
        assert call(context, "initiate", request_id=request_id, files=[{"path": "pending", "size": 1}])["object"] == obj
        pending.append(obj)
    assert pending[0]["reference"]["object_id"] != pending[1]["reference"]["object_id"]
    for index, (store, actor) in enumerate(account_stages):
        result = store.handle(actor, {"operation": "cleanup", "request_id": uuid.uuid4().hex}, now_ms=1001 + cf.UPLOAD_TTL_MS)
        assert result["expired_uploads"] == 1
        assert not path(account_stages[index], pending[index]).exists()
        if index == 0:
            assert path(account_stages[1], pending[1]).exists()
            assert call(account_stages[1], "inspect", reference=pending[1]["reference"])["object"]["status"] == "uploading"


@pytest.mark.parametrize("substitution", ["symlink","hardlink","inode","mode","parent-link"])
def test_substituted_content_is_never_used_or_removed(stage,substitution,tmp_path):
    obj=upload(stage)
    p=path(stage,obj)
    outside=tmp_path/"source-sentinel"
    outside.write_bytes(b"outside")
    outside.chmod(0o400)
    if substitution == "symlink": p.unlink(); p.symlink_to(outside)
    elif substitution == "hardlink": p.unlink(); os.link(outside,p)
    elif substitution == "inode":
        replacement=p.with_name("replacement"); replacement.write_bytes(b"hello"); replacement.chmod(0o400); replacement.replace(p)
    elif substitution == "mode": p.chmod(0o644)
    else:
        parent=p.parent; parent.rename(parent.with_name("saved")); parent.symlink_to(tmp_path,target_is_directory=True)
    for operation in ["attach","download","remove"]:
        with pytest.raises((cf.StagingError,OSError)):
            fields={"target":"task:t"} if operation == "attach" else {"file_index":0,"offset":0} if operation == "download" else {}
            call(stage,operation,reference=obj["reference"],**fields)
    assert outside.read_bytes() == b"outside"


def test_limits_malformed_and_stale_references(stage):
    obj=upload(stage)
    for fields in [{"digest":"a"*64},{"generation":2},{"size":99}]:
        with pytest.raises(cf.StagingError): call(stage,"inspect",reference=dict(obj["reference"],**fields))
    for request in [{"operation":[]},{"operation":"list","server_path":"/etc"},{"operation":"initiate","request_id":"a"*32,"files":[{"path":"x","size":True}]}]:
        with pytest.raises(cf.StagingError): stage[0].handle(stage[1],request,now_ms=1000)
    for files in [[{"path":"x","size":cf.MAX_FILE_BYTES+1}], [{"path":str(n),"size":0} for n in range(cf.MAX_FILES+1)]]:
        with pytest.raises(cf.StagingError): call(stage,"initiate",files=files)
    bad=call(stage,"initiate",files=[{"path":"x","size":1,"digest":"a"*64}])["object"]
    call(stage,"chunk",reference=bad["reference"],file_index=0,offset=0,data="eA==")
    with pytest.raises(cf.StagingError): call(stage,"finalize",reference=bad["reference"])
    with pytest.raises(cf.StagingError): call(stage,"resolve",attachment_id="att_"+"a"*32)


def test_concurrent_same_request_and_attach_remove(stage):
    store,actor=stage
    req={"operation":"initiate","request_id":uuid.uuid4().hex,"files":[{"path":"x","size":0}]}
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(lambda _:store.handle(actor,req,now_ms=1000),range(4)))
    assert len({r["object"]["reference"]["object_id"] for r in results}) == 1
    obj=call(stage,"finalize",reference=results[0]["object"]["reference"])["object"]
    def perform(op):
        try: return call(stage,op,reference=obj["reference"],**({"target":"task:x"} if op == "attach" else {}))
        except cf.StagingError: return None
    with ThreadPoolExecutor(max_workers=2) as executor: results=list(executor.map(perform,["attach","remove"]))
    assert sum(r is not None for r in results) == 1


def test_interrupted_removal_and_finalization_resume(stage,monkeypatch):
    pending=call(stage,"initiate",files=[{"path":"x","size":0},{"path":"y","size":0}])["object"]
    original=os.fchmod
    count=0
    def crash(fd,mode):
        nonlocal count
        count+=1
        if count == 2: raise OSError("disposable interruption")
        return original(fd,mode)
    with monkeypatch.context() as m:
        m.setattr(os,"fchmod",crash)
        with pytest.raises(OSError): call(stage,"finalize",reference=pending["reference"])
    assert call(stage,"inspect",reference=pending["reference"])["object"]["status"] == "finalizing"
    obj=call(stage,"finalize",reference=pending["reference"])["object"]
    original_unlink=os.unlink
    count=0
    def interrupted_unlink(*args,**kwargs):
        nonlocal count
        count+=1
        if count == 2: raise OSError("disposable interruption")
        return original_unlink(*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(os,"unlink",interrupted_unlink)
        with pytest.raises(OSError): call(stage,"remove",reference=obj["reference"])
    assert call(stage,"inspect",reference=obj["reference"])["object"]["status"] == "removing"
    assert call(stage,"remove",reference=obj["reference"])["status"] == "removed"


def test_source_auth_and_sibling_state_preserved(stage):
    store,_=stage
    sentinels={name:store.root.parent/name for name in ["source",".beads","tls","releases","rollback","services"]}
    for p in sentinels.values(): p.mkdir(mode=0o700); (p/"keep").write_bytes(b"sentinel")
    before={p:p.read_bytes() for p in store.auth_directory.iterdir()}
    obj=upload(stage,b"#!/bin/sh\nexit 99",name="script.sh")
    call(stage,"remove",reference=obj["reference"])
    assert all(p.read_bytes() == content for p,content in before.items())
    assert all((p/"keep").read_bytes() == b"sentinel" for p in sentinels.values())


def test_revocation_waits_for_inflight_publication_then_blocks_replay(stage,monkeypatch):
    store,actor=stage
    pending=call(stage,"initiate",files=[{"path":"x","size":0}])["object"]
    entered,release=threading.Event(),threading.Event()
    original=store._verify
    def delayed(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(store,"_verify",delayed)
    def revoke():
        with sqlite3.connect(store.auth_directory/"termdeck-machines-v1.sqlite3",timeout=5) as db:
            db.execute("UPDATE machines SET revoked_at_ms=1")
    with ThreadPoolExecutor(max_workers=2) as executor:
        finalized=executor.submit(call,stage,"finalize",reference=pending["reference"])
        assert entered.wait(5)
        revoked=executor.submit(revoke)
        assert not revoked.done()
        release.set()
        assert finalized.result()["object"]["status"] == "ready"
        revoked.result()
    with pytest.raises(cf.StagingError): call(stage,"list")


def test_allocation_interruption_has_receipt_and_known_partials_cleanup(stage,monkeypatch):
    original=cf.Directory.file
    count=0
    def interrupted(self,relative,**kwargs):
        nonlocal count
        if kwargs.get("create") and relative.endswith(("/x","/y")):
            count+=1
            if count == 2: raise OSError("disposable allocation interruption")
        return original(self,relative,**kwargs)
    request={"operation":"initiate","request_id":uuid.uuid4().hex,"files":[{"path":"x","size":1},{"path":"y","size":1}]}
    with monkeypatch.context() as m:
        m.setattr(cf.Directory,"file",interrupted)
        with pytest.raises(OSError): stage[0].handle(stage[1],request,now_ms=1000)
    assert call(stage,"list")["objects"][0]["status"] == "allocating"
    result=stage[0].handle(stage[1],request,now_ms=1000)
    assert result["replayed"] and result["object"]["status"] == "uploading"
    call(stage,"remove",reference=result["object"]["reference"])
    assert not path(stage,result["object"]).exists()


def test_unreceipted_allocation_is_quarantined_and_never_adopted(stage,monkeypatch):
    original=cf.StagingStore._allocate
    def interrupted(self,db,tree,row,now):
        location=self._location(row)+"/x"
        with tree.file(location,create=True): pass
        raise OSError("death before identity receipt")
    request={"operation":"initiate","request_id":uuid.uuid4().hex,"files":[{"path":"x","size":0}]}
    with monkeypatch.context() as m:
        m.setattr(cf.StagingStore,"_allocate",interrupted)
        with pytest.raises(OSError): stage[0].handle(stage[1],request,now_ms=1000)
    assert cf.StagingStore._allocate == original
    with pytest.raises(FileExistsError): stage[0].handle(stage[1],request,now_ms=1000)
    obj=call(stage,"list")["objects"][0]
    with pytest.raises(cf.StagingError,match="quarantined"): call(stage,"remove",reference=obj["reference"])


def test_refresh_attach_serialization_and_restart(stage):
    obj=upload(stage)
    def refresh(): return upload(stage,b"next",previous=obj["reference"])
    def attach():
        try: return call(stage,"attach",reference=obj["reference"],target="task:x")
        except cf.StagingError: return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(attach); b=executor.submit(refresh)
        attached=a.result(); fresh=b.result()
    restarted=cf.StagingStore(stage[0].root.parent,stage[0].auth_directory)
    assert restarted.handle(stage[1],{"operation":"inspect","reference":fresh["reference"]},now_ms=1000)["object"]["status"] == "ready"
    if attached:
        result=restarted.handle(stage[1],{"operation":"resolve","attachment_id":attached["attachment_id"]},now_ms=1000,internal_resolve=True)
        assert Path(result["server_path"]).joinpath("project/file.txt").read_bytes() == b"hello"


@pytest.mark.parametrize("unsafe", ["owner","device","root-mode","root-symlink","database-hardlink"])
def test_unsafe_storage_root_and_identity_fail_closed(stage,monkeypatch,tmp_path,unsafe):
    store,actor=stage
    obj=upload(stage)
    if unsafe == "owner": monkeypatch.setattr(os,"geteuid",lambda:os.getuid()+1)
    elif unsafe == "device":
        original=store._identity
        def wrong(file,info): original(dict(file,device=-1),info)
        monkeypatch.setattr(store,"_identity",wrong)
    elif unsafe == "root-mode": store.root.chmod(0o755)
    elif unsafe == "root-symlink":
        saved=store.root.with_name("saved"); store.root.rename(saved); store.root.symlink_to(saved,target_is_directory=True)
    else: os.link(store.root/"metadata.sqlite3",tmp_path/"linked-db")
    with pytest.raises((cf.StagingError,OSError)):
        call(stage,"download",reference=obj["reference"],file_index=0,offset=0)


@pytest.mark.parametrize("owner,mode,accepted", [
    ("current", 0o755, True),
    ("root", 0o755, True),
    ("current", 0o770, False),
    ("root", 0o777, False),
    ("root", 0o1777, True),
    ("current", 0o1777, os.geteuid() == 0),
    ("foreign", 0o755, False),
    ("foreign", 0o1777, False),
])
def test_storage_ancestor_ownership_and_temporary_parent_policy(tmp_path, monkeypatch, owner, mode, accepted):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    private = ancestor / "private"
    private.mkdir(mode=0o700)
    identity = ancestor.stat()
    uid = {"current": os.geteuid(), "root": 0, "foreign": os.geteuid() + 100000}[owner]
    original_stat, original_fstat = os.stat, os.fstat

    def observed(info):
        if (info.st_dev, info.st_ino) != (identity.st_dev, identity.st_ino):
            return info
        values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")}
        return SimpleNamespace(**dict(values, st_uid=uid, st_mode=stat.S_IFDIR | mode))

    # Vary only the named ancestor's metadata; the private leaf and effective
    # principal remain unchanged. No real ownership or system path is changed.
    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", lambda *args, **kwargs: observed(original_stat(*args, **kwargs)))
        patch.setattr(os, "fstat", lambda *args, **kwargs: observed(original_fstat(*args, **kwargs)))
        if accepted:
            directory = cf.Directory(private)
            try:
                directory.check()
            finally:
                directory.close()
        else:
            with pytest.raises(cf.StagingError) as rejected:
                cf.Directory(private)
            assert rejected.value.code == "unsafe-storage"


@pytest.mark.parametrize("change", ["permissions", "replacement"])
def test_directory_revalidation_detects_changed_ancestor_with_same_leaf(tmp_path, change):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    private = ancestor / "private"
    private.mkdir(mode=0o700)
    directory = cf.Directory(private)
    try:
        if change == "permissions":
            ancestor.chmod(0o750)
        else:
            saved = ancestor.with_name("saved")
            ancestor.rename(saved)
            ancestor.mkdir(mode=0o755)
            (saved / "private").rename(private)
        assert private.stat().st_ino == directory.identity.st_ino
        with pytest.raises(cf.StagingError) as rejected:
            directory.check()
        assert rejected.value.code == "unsafe-storage"
    finally:
        directory.close()


def test_reused_staging_store_keeps_state_parent_private(stage):
    store, actor = stage
    call(stage, "list")
    store.root.parent.chmod(0o755)
    with pytest.raises(cf.StagingError) as rejected:
        call(stage, "list")
    assert rejected.value.code == "unsafe-storage"
    with pytest.raises(cf.StagingError) as rejected:
        cf.StagingStore(store.root.parent, store.auth_directory)
    assert rejected.value.code == "unsafe-storage"
    store.root.parent.chmod(0o700)
    assert store.handle(actor, {"operation": "list"}, now_ms=1000)["ok"]


@pytest.mark.parametrize("replay", [False, True])
def test_request_revalidates_ancestry_before_success(stage, monkeypatch, replay):
    store, actor = stage
    request = {"operation": "initiate", "request_id": uuid.uuid4().hex, "files": [{"path": "empty", "size": 0}]}
    store.handle(actor, request, now_ms=1000)
    method = "_row" if replay else "_operate"
    original = getattr(store, method)

    def drift(*args, **kwargs):
        result = original(*args, **kwargs)
        store.root.parent.chmod(0o755)
        return result

    monkeypatch.setattr(store, method, drift)
    with pytest.raises(cf.StagingError) as rejected:
        store.handle(actor, request if replay else {"operation": "list"}, now_ms=1000)
    assert rejected.value.code == "unsafe-storage"


def test_namespace_quota_and_object_size_are_enforced(stage,monkeypatch):
    with pytest.raises(cf.StagingError): call(stage,"initiate",files=[{"path":str(i),"size":cf.MAX_FILE_BYTES} for i in range(5)])
    monkeypatch.setattr(cf,"MAX_NAMESPACE_BYTES",3)
    with pytest.raises(cf.StagingError): upload(stage,b"four")
    monkeypatch.setattr(cf,"MAX_OBJECTS",0)
    with pytest.raises(cf.StagingError): upload(stage,b"")


def test_concurrent_chunk_remove_cannot_republish_removed_content(stage):
    pending=call(stage,"initiate",files=[{"path":"x","size":1}])["object"]
    def write():
        try: return call(stage,"chunk",reference=pending["reference"],file_index=0,offset=0,data="eA==")
        except cf.StagingError: return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        chunk=executor.submit(write)
        removed=executor.submit(call,stage,"remove",reference=pending["reference"])
        chunk.result()
        assert removed.result()["status"] == "removed"
    assert call(stage,"inspect",reference=pending["reference"])["object"]["status"] == "removed"
    with pytest.raises(cf.StagingError): call(stage,"finalize",reference=pending["reference"])
    assert not path(stage,pending).exists()


@pytest.mark.parametrize("kind", ["symlink","hardlink"])
def test_database_sidecar_substitution_preserves_outside_file(stage,kind):
    store,_=stage
    upload(stage)
    outside=store.root.parent/"outside-journal"; outside.write_bytes(b"preserve"); outside.chmod(0o600)
    sidecar=store.root/"metadata.sqlite3-journal"
    if kind=="symlink": sidecar.symlink_to(outside)
    else: os.link(outside,sidecar)
    with pytest.raises((cf.StagingError,OSError)): call(stage,"list")
    assert outside.read_bytes()==b"preserve"
