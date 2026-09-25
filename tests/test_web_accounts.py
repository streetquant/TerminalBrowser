from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import uuid

import pytest

from termdeck.codex_auth import CodexAuthManager
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import AuthActivationPlan, AuthActivationResult, AuthQuiesceResult
from termdeck.web_accounts import handle
from termdeck.web_management import ManagementError
from test_codex_auth import _write_native_source, _bind_native_source_default


class StoppedBackend:
    def __init__(self): self.calls=[]
    def plan_auth_change(self, record): return AuthActivationPlan(True)
    def quiesce_auth_change(self, record, plan): return AuthQuiesceResult(True)
    def apply_auth_change(self, record, plan, quiescence):
        self.calls.append(record.id)
        return AuthActivationResult(True,False,status="stopped")


@pytest.fixture
def accounts(tmp_path):
    source=tmp_path/"termdeck-accounts"; _write_native_source(source); _bind_native_source_default(source)
    store=SessionStore(tmp_path/"sessions.sqlite3",account_source=source)
    rows=[store.create(f"Tab {i}","test","bash",str(tmp_path)) for i in range(2)]
    rows=[store.update(row.id,status="stopped") for row in rows]
    manager=CodexAuthManager(account_source=source)
    backend=StoppedBackend()
    yield store,rows,manager,backend
    store.close()


def request(fixture, ids=None, **changes):
    store,rows,manager,_=fixture
    selected=ids if ids is not None else [rows[0].id]
    expected={row.id:manager.state(row).fingerprint for row in rows if row.id in selected}
    return {"operation":"switch-account","ids":selected,"confirmation":selected,"account":"beta",
            "expected":expected,"request_id":uuid.uuid4().hex,"_actor_session":"authenticated-session",**changes}


def run(fixture, req):
    store,_,manager,backend=fixture
    return handle(store,req,manager=manager,backend=backend)


def test_one_tab_switch_replay_and_canonical_independence(accounts):
    store,rows,manager,backend=accounts
    source=manager.account_source
    before=(Path(rows[1].codex_home)/"auth.json").read_bytes()
    req=request(accounts)
    result=run(accounts,req)
    assert result["ok"] and backend.calls==[rows[0].id]
    assert run(accounts,req)==result and backend.calls==[rows[0].id]
    assert manager.state(rows[0]).selected_account_key=="beta"
    assert manager.state(rows[0]).canonical_account_key=="alpha"
    assert (Path(rows[1].codex_home)/"auth.json").read_bytes()==before
    assert (source/"current").read_text()=="alpha\n"
    public=run(accounts,{"operation":"accounts"})
    assert public["rows"][0]["running_account_state"]=="stopped"
    assert "alpha-refresh" not in json.dumps(public) and "beta-refresh" not in json.dumps(public)
    with pytest.raises(ManagementError,match="different values"): run(accounts,{**req,"account":"alpha"})


def test_global_requires_exact_confirmation_and_scope_then_switches_both(accounts):
    _,rows,manager,backend=accounts
    ids=[row.id for row in rows]
    req=request(accounts,ids,operation="switch-all-accounts")
    with pytest.raises(ManagementError,match="Confirm"): run(accounts,{**req,"confirmation":[]})
    with pytest.raises(ManagementError,match="scope changed"):
        run(accounts,request(accounts,operation="switch-all-accounts"))
    assert not backend.calls
    assert run(accounts,req)["ok"]
    assert backend.calls==ids
    assert all(manager.state(row).selected_account_key=="beta" for row in rows)


def test_state_change_and_account_deletion_never_overwrite(accounts):
    _,rows,manager,backend=accounts
    req=request(accounts)
    (Path(rows[0].codex_home)/"current").write_text("gamma\n")
    result=run(accounts,req)
    assert not result["ok"] and not backend.calls
    fresh=request(accounts)
    (manager.account_source/"accounts/beta.json").unlink()
    result=run(accounts,fresh)
    assert not result["ok"] and not backend.calls
    assert manager.state(rows[0]).selected_account_key=="alpha"


def test_backend_refusal_preserves_scope_and_reports_failure(accounts):
    class Refuse(StoppedBackend):
        def plan_auth_change(self,record): return AuthActivationPlan(False,detail="Exact running conversation unavailable")
    store,rows,manager,_=accounts
    backend=Refuse()
    result=handle(store,request(accounts),manager=manager,backend=backend)
    assert not result["ok"] and not backend.calls
    assert manager.state(rows[0]).selected_account_key=="alpha"
    assert "Exact running" in result["results"][0]["error"]


@pytest.mark.parametrize("changes",[{"_actor_session":None},{"request_id":"bad"},{"ids":[]},{"ids":[1]},{"expected":{}},{"account":None}])
def test_malformed_switch_never_mutates(accounts,changes):
    with pytest.raises(ManagementError): run(accounts,request(accounts,**changes))
    assert not accounts[3].calls


def test_repeated_switch_and_new_manager_keep_private_selection(accounts):
    _,rows,manager,backend=accounts
    for selected in ("beta","alpha","beta"):
        assert run(accounts,request(accounts,account=selected))["ok"]
        restarted=CodexAuthManager(account_source=manager.account_source)
        assert restarted.state(rows[0]).selected_account_key==selected
    assert len(backend.calls)==3


def test_concurrent_duplicate_requests_apply_once(accounts):
    store,rows,manager,backend=accounts
    req=request(accounts)
    def switch(_):
        separate=SessionStore(store.path)
        try: return handle(separate,req,manager=manager,backend=backend)
        finally: separate.close()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(switch,range(2)))
    assert any(row["ok"] for row in results)
    assert backend.calls==[rows[0].id]
    assert run(accounts,req)["ok"]


def test_rotated_private_snapshot_synchronizes_before_switch_without_reverting_tokens(accounts):
    from test_codex_credentials import payload, write
    _,rows,manager,_=accounts
    source=manager.account_source
    write(source/"accounts/alpha.json",payload())
    write(source/".termdeck-account-authority.json",{"version":1,"owner":"termdeck","refresh_writes":True})
    home=Path(rows[0].codex_home)
    (home/"auth.json").unlink()
    write(home/"auth.json",payload(refresh="2026-03-01T00:00:00Z",secret="refreshed-fixture"))
    newest=(home/"auth.json").read_bytes()
    assert manager.state(rows[0]).credential_status=="refreshed"
    result=run(accounts,request(accounts))
    assert result["ok"],result
    assert (source/"accounts/alpha.json").read_bytes()==newest
    assert manager.state(rows[0]).selected_account_key=="beta"
    assert manager.state(rows[1]).selected_account_key=="alpha"
    assert (Path(rows[1].codex_home)/"auth.json").read_bytes()==newest


def test_failed_runtime_receipt_is_not_reported_as_success(accounts,monkeypatch):
    _,_,manager,_=accounts
    def fail(*_): raise OSError("fixture receipt failure")
    monkeypatch.setattr(manager,"record_activation",fail)
    result=run(accounts,request(accounts))
    assert not result["ok"]
    assert result["results"][0]["state"]["auth_restart_required"]
