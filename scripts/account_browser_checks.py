"""TermDeck account UI/API proof using synthetic credentials in disposable state."""
from pathlib import Path
import hashlib
import json
import uuid

from playwright.sync_api import expect, sync_playwright
from termdeck.session_store import SessionStore


def check_accounts(*, origin, cookies, root, output, assertions, post):
    output.mkdir(parents=True,exist_ok=True)
    source=root/"termdeck-account-source"; source.mkdir(mode=0o700)
    (source/"accounts").mkdir(mode=0o700)
    for name in ("alpha","beta"):
        path=source/"accounts"/f"{name}.json"
        path.write_text(json.dumps({"token":f"synthetic-{name}"})); path.chmod(0o600)
    (source/"auth.json").symlink_to("accounts/alpha.json")
    (source/"current").write_text("alpha\n"); (source/"current").chmod(0o600)
    executable=root/"bin/codex-fixture-unused"
    executable.write_text("#!/bin/sh\nexit 99\n"); executable.chmod(0o700)
    policy=root/"state/web-launch-policy.json"; value=json.loads(policy.read_text())
    value["codex"]={"executable":str(executable),"sha256":hashlib.sha256(executable.read_bytes()).hexdigest(),"account_source":str(source)}
    policy.write_text(json.dumps(value))
    store=SessionStore(root/"state/sessions.sqlite3",account_source=source,provision_auth_scopes=False)
    rows=[]
    try:
        for name in ("Account A","Account B"):
            row=store.create(name,"fixture","/bin/bash",str(root/"workspace"),window_id="accounts-fixture",window_title="Account fixture")
            row=store.ensure_auth_scope(row.id)
            rows.append(store.update(row.id,status="stopped",visible=False))
    finally: store.close()
    def get():
        status,result=post("management",{"operation":"accounts"}); assert status==200 and result["ok"],result
        return result
    listed=get(); ids=[row.id for row in rows]
    request={"operation":"switch-account","request_id":uuid.uuid4().hex,"ids":[ids[0]],"confirmation":[ids[0]],"account":"beta","expected":{ids[0]:next(row["fingerprint"] for row in listed["rows"] if row["id"]==ids[0])}}
    assert post("management",request,login=False)[0]==401
    assert post("management",request,csrf_ok=False)[0]==403
    assert post("management",dict(request,_actor_session="forged"))[0]==400
    status,result=post("management",request); assert status==200 and result["ok"],result
    assert post("management",request)[1]==result
    by_id={row["id"]:row for row in get()["rows"]}
    assert by_id[ids[0]]["effective_account_key"]=="beta" and by_id[ids[1]]["effective_account_key"]=="alpha"
    assert by_id[ids[0]]["canonical_account_key"]=="alpha"
    assert "synthetic-alpha" not in json.dumps(by_id) and "synthetic-beta" not in json.dumps(by_id)
    assertions["account_http_auth_csrf_replay_one_tab_and_no_credential_leak"]=True
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path="/usr/bin/chromium",headless=True)
        context=browser.new_context(viewport={"width":1280,"height":1000}); context.add_cookies(cookies)
        page=context.new_page(); errors=[]; page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(origin+"/"); page.get_by_role("button",name="Codex accounts",exact=True).click()
        modal=page.locator("#management-dialog")
        first=modal.locator(f'.account-row[data-session-id="{ids[0]}"]')
        expect(first).to_contain_text("Canonical account: alpha")
        expect(first).to_contain_text("effective credentials: beta")
        expect(first).to_contain_text("stopped")
        # Confirmation cancellation has no process or credential effects.
        page.once("dialog",lambda dialog:dialog.dismiss())
        modal.get_by_role("button",name="Switch all tabs…",exact=True).click()
        assert {row["id"]:row["effective_account_key"] for row in get()["rows"]}[ids[0]]=="beta"
        modal.get_by_label("Account for all tabs",exact=True).select_option("beta")
        confirmations=[]
        def confirm(dialog): confirmations.append(dialog.message); dialog.accept()
        page.once("dialog",confirm)
        modal.get_by_role("button",name="Switch all tabs…",exact=True).click()
        expect(modal).to_contain_text("transactions succeeded",timeout=20000)
        assert len(confirmations)==1 and all(item in confirmations[0] for item in ids)
        assert all(row["effective_account_key"]=="beta" for row in get()["rows"])
        assert (source/"current").read_text()=="alpha\n"
        page.screenshot(path=str(output/"accounts-distinct-state.png"),full_page=True)
        assert not errors,errors
        context.close(); browser.close()
    assertions["account_browser_effective_canonical_restart_display_and_confirmed_global_switch"]=True
