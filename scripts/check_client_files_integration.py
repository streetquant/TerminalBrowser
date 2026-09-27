#!/usr/bin/env python3
"""Real HTTP -> Rust authentication -> Python staging proof in disposable state."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_web_terminal_browser import free_port, invitation, pair_cookies, wait_http
from termdeck.session_store import SessionStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", action="store_true", help="also run rendered disposable Chromium interactions")
    args = parser.parse_args()
    binary=args.binary.resolve()
    assertions={}
    with tempfile.TemporaryDirectory(prefix="t1000-stage-integration-") as temporary:
        root=Path(temporary)
        for child in ("state","auth","home","tmux","bin","workspace"):
            (root/child).mkdir(mode=0o700)
        (root/"home/.codex").mkdir(mode=0o700)
        database=root/"state/sessions.sqlite3"
        SessionStore(database).close()
        policy={"version":1,"workspaces":[{"id":"ws_"+"a"*32,"label":"Disposable approved workspace","path":str(root/"workspace")}],"codex":None}
        policy_path=root/"state/web-launch-policy.json"
        policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o600)
        wrapper=root/"bin/t1000"
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m termdeck.cli \"$@\"\n")
        wrapper.chmod(0o700)
        env={**os.environ,"HOME":str(root/"home"),"SHELL":"/bin/bash","CODEX_HOME":str(root/"home/.codex"),"TERMDECK_CODEX_ACCOUNT_SOURCE":str(root/"home/.codex"),"XDG_DATA_HOME":str(root/"home/data"),"XDG_CONFIG_HOME":str(root/"home/config"),"TMUX_TMPDIR":str(root/"tmux"),"PYTHONPATH":str(ROOT),"PYTHONNOUSERSITE":"1"}
        for key in ("TMUX","TMUX_PANE","TERMDECK_STATE_ROOT","TERMDECK_TMUX_RUNTIME","TERMDECK_TMUX_IDENTITY"):
            env.pop(key,None)
        port=free_port()
        origin=f"http://127.0.0.1:{port}"
        invite=invitation(binary,root/"auth","Staging integration browser")
        with (root/"server.log").open("wb") as log:
            server=subprocess.Popen([str(binary),"--db",str(database),"--listen",f"127.0.0.1:{port}","--auth-dir",str(root/"auth"),"--host-id","host-browser-e2e","--owner-id","owner-browser-e2e","--allow-insecure-loopback-auth","--t1000",str(wrapper),"--tmux","/usr/bin/tmux","--tab-action-timeout-ms","30000"],cwd=ROOT,env=env,stdout=log,stderr=log)
        try:
            wait_http(origin+"/_termdeck/healthz")
            cookies=pair_cookies(origin,invite)
            cookie="; ".join(f"{c['name']}={c['value']}" for c in cookies)
            csrf=next(c["value"] for c in cookies if c["name"] == "termdeck_csrf")
            def post(route,body,*,key=None,login=True,csrf_ok=True):
                headers={"Content-Type":"application/json","Origin":origin,"Sec-Fetch-Site":"same-origin","Sec-Fetch-Mode":"cors"}
                if login: headers["Cookie"]=cookie
                if csrf_ok: headers["X-TermDeck-CSRF"]=csrf
                if key is not None: headers["X-TermDeck-Machine-Key"]=key
                request=urllib.request.Request(origin+"/_termdeck/api/v1/"+route,data=json.dumps(body).encode(),headers=headers,method="POST")
                try:
                    with urllib.request.urlopen(request,timeout=35) as response:
                        return response.status,json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code,json.load(error)
            def stage(operation,*,key=None,**fields):
                if operation in {"initiate","finalize","remove","attach","detach","cleanup"}: fields.setdefault("request_id",uuid.uuid4().hex)
                return post("client-files",{"operation":operation,**fields},key=key)
            def expect(response,status=200):
                assert response[0] == status, (response[0], response[1].get("error"))
                return response[1]
            a="tdm1_"+secrets.token_hex(32)
            b="tdm1_"+secrets.token_hex(32)
            machine_a=expect(post("machines",{"operation":"enroll","credential":a,"label":"A","request_id":uuid.uuid4().hex}))["machine"]
            expect(post("machines",{"operation":"enroll","credential":b,"label":"B","request_id":uuid.uuid4().hex}))
            expect(post("client-files",{"operation":"list"},key=a,login=False),401)
            expect(post("client-files",{"operation":"list"},key=a,csrf_ok=False),403)
            expect(stage("list"),403)
            assertions["login_csrf_and_enrolled_machine_required"]=True
            empty={"operation":"create-window","name":"Empty integration","request_id":uuid.uuid4().hex}
            expect(post("management",empty,login=False),401)
            expect(post("management",empty,csrf_ok=False),403)
            for invalid in ({"operation":"create-window","name":"missing receipt"},dict(empty,_actor_session="forged"),dict(empty,request_id="invalid")):
                expect(post("management",invalid),400)
            with ThreadPoolExecutor(max_workers=4) as pool:
                empty_results=list(pool.map(lambda _:post("management",empty),range(4)))
            empty_id=expect(empty_results[0])["window_id"]
            assert all(expect(result)["window_id"]==empty_id for result in empty_results)
            assert expect(post("management",empty))["replayed"]
            assert not expect(post("management",dict(empty,name="different")))["ok"]
            assertions["empty_window_auth_csrf_concurrent_creation_and_durable_replay"]=True
            body={"operation":"initiate","request_id":uuid.uuid4().hex,"label":"client directory","display_path":"/home/client/project","files":[{"path":"project/a.txt","size":5,"digest":hashlib.sha256(b"hello").hexdigest()},{"path":"project/empty","size":0}]}
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses=list(pool.map(lambda _:post("client-files",body,key=a),range(2)))
            first=expect(responses[0])["object"]
            assert expect(responses[1])["object"]["reference"] == first["reference"]
            expect(post("client-files",dict(body,label="conflict"),key=a),409)
            assertions["concurrent_initiation_replay"]=True
            reference=first["reference"]
            assert expect(stage("list",key=b))["objects"] == []
            expect(stage("inspect",key=b,reference=reference),403)
            expect(stage("finalize",key=a,reference=reference),409)
            chunk=dict(reference=reference,file_index=0,offset=0,data=base64.b64encode(b"hello").decode())
            expect(stage("chunk",key=a,**chunk)); expect(stage("chunk",key=a,**chunk))
            ready=expect(stage("finalize",key=a,reference=reference,request_id="a"*32))["object"]
            assert expect(stage("finalize",key=a,reference=reference,request_id="a"*32))["object"] == ready
            reference=ready["reference"]
            assert base64.b64decode(expect(stage("download",key=a,reference=reference,file_index=0,offset=0))["data"]) == b"hello"
            assert str(root) not in json.dumps(ready)
            assertions["directory_digest_atomic_publication_chunk_replay"]=True
            for operation, fields in [("inspect",{}),("download",{"file_index":0,"offset":0}),("remove",{}),("attach",{"target":"task:integration"})]:
                expect(stage(operation,key=b,reference=reference,**fields),403)
            expect(stage("resolve",key=a,attachment_id="att_"+"a"*32),400)
            expect(stage("initiate",key=a,files=[{"path":"../escape","size":0}]),400)
            expect(stage("initiate",key=a,files=[{"path":"x","size":0}],server_path=str(root/"auth")),400)
            expect(stage("initiate",key=a,files=[{"path":"x","size":17*1024*1024}]),413)
            assertions["machine_scoped_api_and_upload_destination_validation"]=True
            attached=expect(stage("attach",key=a,reference=reference,target="task:integration"))
            expect(stage("remove",key=a,reference=reference),409)
            expect(stage("detach",key=a,reference=reference,attachment_id=attached["attachment_id"]))
            options=expect(post("management",{"operation":"launch-options"},key=a))
            assert options["server_directory"] is True
            assert next(item["label"] for item in options["runtimes"] if item["id"]=="shell")=="Native shell"
            create={"operation":"create-window","request_id":uuid.uuid4().hex,"name":"Native integration","workspace_id":policy["workspaces"][0]["id"],"policy_revision":options["policy_revision"],"runtime":"shell","tabs":["Initial shell"],"attachments":[reference]}
            expect(post("management",create,key=a,login=False),401)
            expect(post("management",create,key=a,csrf_ok=False),403)
            expect(post("management",create,key=b),403)
            for changed in ({"runtime":"unsupported"},{"workspace_id":"/etc"},{"command":"touch /tmp/forbidden"},{"name":"\u0000"},{"tabs":[]}):
                expect(post("management",dict(create,request_id=uuid.uuid4().hex,**changed),key=a),400)
            expect(post("management",dict(create,policy_revision="stale"),key=a),409)
            with ThreadPoolExecutor(max_workers=2) as pool:
                creations=list(pool.map(lambda _:post("management",create,key=a),range(2)))
            created=expect(creations[0])
            assert expect(creations[1])["window_id"]==created["window_id"]
            assert expect(post("management",create,key=a))["window_id"]==created["window_id"]
            expect(post("management",dict(create,name="changed payload"),key=a),409)
            assert created["status"]=="started"
            sessions=SessionStore(database)
            record=sessions.get(created["tabs"][0]); sessions.close()
            assert record.cwd == str(root/"workspace")
            socket=root/"tmux"/f"tmux-{os.geteuid()}"/"default"
            def check_shell_output(record,command,proof,expected):
                # Input is restricted to this disposable, newly created pane.
                target=f"{record.tmux_name}:0.0"
                time.sleep(.3)
                subprocess.run(["/usr/bin/tmux","-S",str(socket),"send-keys","-t",target,"-l",command],check=True,timeout=3,capture_output=True)
                subprocess.run(["/usr/bin/tmux","-S",str(socket),"send-keys","-t",target,"Enter"],check=True,timeout=3,capture_output=True)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if proof.exists() and proof.read_bytes()==expected: return
                    time.sleep(.05)
                captured=subprocess.run(["/usr/bin/tmux","-S",str(socket),"capture-pane","-pt",target],capture_output=True,text=True,timeout=3)
                raise AssertionError("Disposable native shell proof failed: "+captured.stdout[-2000:])
            host_path=root/"state/client-files"/reference["account_id"]/reference["machine_id"]/"objects"/reference["object_id"]/f"g{reference['generation']}"/"content/project/a.txt"
            command="cat "+shlex.quote(str(host_path))+" > "+shlex.quote(str(root/"workspace/attachment-proof"))
            check_shell_output(record,command,root/"workspace/attachment-proof",b"hello")
            assertions["configured_window_replay_tmux_and_canonical_attachment_read"]=True
            outside=root/"host-user-readable-outside-workspace.txt"
            outside_bytes=b"owned native host terminal proof\n"
            outside.write_bytes(outside_bytes); outside.chmod(0o600)
            assert root/"workspace" not in outside.parents
            check_shell_output(record,"cat "+shlex.quote(str(outside))+" > "+shlex.quote(str(root/"workspace/host-user-proof")),root/"workspace/host-user-proof",outside_bytes)
            assert outside.read_bytes()==outside_bytes
            assertions["native_host_shell_reads_owned_file_outside_starting_directory"]=True

            server_directory=root/"server project with spaces"
            server_directory.mkdir(mode=0o700)
            assert str(server_directory) not in {item["path"] for item in options["workspaces"]}
            explicit={key:value for key,value in create.items() if key!="workspace_id"}
            explicit.update(request_id=uuid.uuid4().hex,name="Server directory integration",workspace_path=str(server_directory))
            def registry():
                sessions=SessionStore(database,provision_auth_scopes=False)
                try:
                    return (dict(sessions.remembered_windows()),
                            {row.id:(row.window_id,row.name,row.command,row.cwd) for row in sessions.list()})
                finally: sessions.close()
            before_invalid=registry()
            expect(post("management",explicit,key=a,login=False),401)
            expect(post("management",explicit,key=a,csrf_ok=False),403)
            expect(post("management",explicit),403)
            expect(post("management",explicit,key=b),403)
            expect(post("management",dict(explicit,workspace_id=create["workspace_id"]),key=a),400)
            missing_selector=expect(post("management",{key:value for key,value in explicit.items() if key!="workspace_path"},key=a))
            assert not missing_selector["ok"] and missing_selector["error"]=="Unknown request field"
            assert registry()==before_invalid
            inaccessible=root/"inaccessible server directory"
            inaccessible.mkdir(mode=0o700)
            inaccessible.chmod(0)
            try:
                assert not os.access(inaccessible,os.R_OK|os.X_OK,effective_ids=True), "Inaccessible-directory proof requires an unprivileged fixture account"
                for invalid_path in (root/"missing server directory",inaccessible):
                    refused=expect(post("management",dict(explicit,workspace_path=str(invalid_path)),key=a),400)
                    assert refused["code"]=="unavailable-workspace"
                    assert "Server directory" in refused["error"]
                    assert any(word in refused["error"] for word in ("missing","inaccessible","readable","searchable"))
                    assert registry()==before_invalid
            finally: inaccessible.chmod(0o700)
            assertions["server_directory_auth_csrf_machine_selector_and_clear_refusal"]=True
            with ThreadPoolExecutor(max_workers=2) as pool:
                explicit_results=list(pool.map(lambda _:post("management",explicit,key=a),range(2)))
            explicit_created=expect(explicit_results[0])
            assert expect(explicit_results[1])["window_id"]==explicit_created["window_id"]
            assert expect(post("management",explicit,key=a))["window_id"]==explicit_created["window_id"]
            expect(post("management",dict(explicit,workspace_path=str(root/"workspace")),key=a),409)
            assert explicit_created["status"]=="started"
            sessions=SessionStore(database,provision_auth_scopes=False)
            try:
                explicit_record=sessions.get(explicit_created["tabs"][0])
                assert explicit_record.cwd==str(server_directory)
                assert len(sessions.list(window_id=explicit_created["window_id"]))==1
            finally: sessions.close()
            after_explicit=registry()
            assert all(after_explicit[0].get(key)==value for key,value in before_invalid[0].items())
            assert all(after_explicit[1].get(key)==value for key,value in before_invalid[1].items())
            check_shell_output(explicit_record,"pwd > "+shlex.quote(str(server_directory/"cwd-proof")),server_directory/"cwd-proof",(str(server_directory)+"\n").encode())
            check_shell_output(explicit_record,"cat "+shlex.quote(str(host_path))+" > "+shlex.quote(str(server_directory/"attachment-proof")),server_directory/"attachment-proof",b"hello")
            assert ready["display_path"]=="/home/client/project"
            assert explicit_record.cwd != ready["display_path"]
            assertions["unregistered_server_directory_spaces_concurrent_replay_and_native_cwd"]=True
            for link in [*created["attachments"],*explicit_created["attachments"]]:
                expect(stage("detach",key=a,reference=link["reference"],attachment_id=link["attachment_id"]))
            expect(stage("remove",key=a,reference=reference))
            expect(stage("download",key=a,reference=reference,file_index=0,offset=0),409)
            assertions["attachments_retain_content_until_detached"]=True
            expect(post("machines",{"operation":"revoke","machine_id":machine_a["machine_id"],"request_id":uuid.uuid4().hex}))
            expect(stage("list",key=a),403)
            expect(stage("list",key=b))
            assertions["revocation_blocks_machine_and_preserves_other_machine"]=True
            if args.browser:
                from scripts.account_browser_checks import check_accounts
                check_accounts(origin=origin,cookies=cookies,root=root,output=args.output.parent/"browser-accounts",assertions=assertions,post=post)
                from scripts.client_files_browser_checks import check_client_files
                check_client_files(origin=origin,cookies=cookies,root=root,output=args.output.parent/"browser-files",assertions=assertions)
            import sqlite3
            with sqlite3.connect(root/"auth/termdeck-web-auth-v1.sqlite3") as expired:
                expired.execute("UPDATE auth_sessions SET expires_at_ms=1")
            expect(post("management",dict(create,request_id=uuid.uuid4().hex),key=b),401)
            expect(post("management",dict(explicit,request_id=uuid.uuid4().hex),key=b),401)
            assertions["expired_session_cannot_create_window"]=True
            assert all(p.stat().st_mode & 0o077 == 0 for p in (root/"state/client-files").rglob("*") if not p.is_symlink())
            assertions["private_storage_modes"]=True
        finally:
            server.terminate()
            server.wait(timeout=10)
            subprocess.run(["/usr/bin/tmux","-S",str(root/"tmux"/f"tmux-{os.geteuid()}"/"default"),"kill-server"],capture_output=True,timeout=3)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"assertions":assertions,"binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),"production_actions":[]},indent=2)+"\n")
    print(f"{len(assertions)}/{len(assertions)} integration assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
