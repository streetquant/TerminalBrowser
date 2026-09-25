import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from termdeck.codex_credentials import CredentialError, activation_receipt, observe, read, runtime_observation, sync_quiescent


def payload(account="A", user="user-A", refresh="2026-01-01T00:00:00Z", secret="fixture-token"):
    claims={"sub":user,"iss":"fixture","https://api.openai.com/auth":{"chatgpt_account_id":account}}
    encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"auth_mode":"chatgpt","last_refresh":refresh,"tokens":{"id_token":"e30."+encoded+".fixture", "account_id":account,"refresh_token":secret}}


def write(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)


@pytest.fixture
def catalog(tmp_path):
    source=tmp_path/"termdeck-source"; source.mkdir(mode=0o700)
    accounts=source/"accounts"; accounts.mkdir(mode=0o700)
    home=tmp_path/"private"; home.mkdir(mode=0o700)
    write(accounts/"a.json",payload())
    write(accounts/"b.json",payload("B","user-B"))
    write(source/".termdeck-account-authority.json",{"version":1,"owner":"termdeck","refresh_writes":True})
    return home, source, {"a":accounts/"a.json","b":accounts/"b.json"}


def test_content_identifies_regular_rotated_auth_without_marker(catalog):
    home, _, credentials=catalog
    (home/"current").write_text("b")
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z",secret="new-fixture-token"))
    result=observe(home,credentials)
    assert (result.key,result.status)==("a","refreshed")


def test_refresh_saves_newer_only_and_preserves_other_accounts(catalog):
    home, source, credentials=catalog
    other=credentials["b"].read_bytes()
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z",secret="new-fixture-token"))
    assert sync_quiescent(home,source,credentials)=="synchronized"
    assert credentials["a"].read_bytes()==(home/"auth.json").read_bytes()
    assert sync_quiescent(home,source,credentials)=="current"
    assert credentials["b"].read_bytes()==other
    assert credentials["a"].stat().st_mode & 0o777 == 0o600


def test_stale_or_equal_time_conflict_preserves_both_files(catalog):
    home, source, credentials=catalog
    for refresh, expected in [("2025-01-01T00:00:00Z","stale"),("2026-01-01T00:00:00Z","conflict")]:
        write(home/"auth.json",payload(refresh=refresh,secret="different"))
        before=[p.read_bytes() for p in [home/"auth.json",credentials["a"]]]
        assert observe(home,credentials).status==expected
        with pytest.raises(CredentialError,match=expected): sync_quiescent(home,source,credentials)
        assert [p.read_bytes() for p in [home/"auth.json",credentials["a"]]]==before


@pytest.mark.parametrize("content", ["{", "[]", '{"tokens":null}', '{"tokens":{"refresh_token":0}}', '{"token":"x","token":"y"}', '{"token":"x","last_refresh":"yesterday"}', '{"token":"x","last_refresh":"2026-01-01T00:00:00"}'])
def test_malformed_files_never_fall_back_to_marker(catalog,content):
    home, source, credentials=catalog
    (home/"auth.json").write_text(content); (home/"current").write_text("a")
    assert observe(home,credentials).key is None
    with pytest.raises(CredentialError,match="malformed"): sync_quiescent(home,source,credentials)
    assert (home/"auth.json").read_text()==content


def test_refresh_requires_explicit_termdeck_authority(catalog):
    home, source, credentials=catalog
    (source/".termdeck-account-authority.json").unlink()
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z"))
    old=credentials["a"].read_bytes()
    with pytest.raises(CredentialError,match="policy is required"): sync_quiescent(home,source,credentials)
    assert credentials["a"].read_bytes()==old


def test_legacy_private_catalogue_requires_safe_content_match(catalog):
    home, _, credentials=catalog
    accounts=home/"accounts"; accounts.mkdir(mode=0o700)
    write(accounts/"legacy.json",payload())
    (home/"auth.json").symlink_to("accounts/legacy.json")
    assert (observe(home,credentials).key,observe(home,credentials).status)==("a","current")
    write(accounts/"legacy.json",payload(refresh="2025-01-01T00:00:00Z"))
    assert observe(home,credentials).status=="stale"
    accounts.chmod(0o777)
    assert observe(home,credentials).key is None
    accounts.chmod(0o700)
    (home/"auth.json").unlink()
    foreign=home.parent/"foreign.json"; write(foreign,payload())
    (home/"auth.json").symlink_to(foreign)
    assert observe(home,credentials).key is None


def test_refresh_rejects_nonprivate_authority_policy(catalog):
    home, source, credentials=catalog
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z"))
    (source/".termdeck-account-authority.json").chmod(0o644)
    with pytest.raises(CredentialError,match="policy is required"):
        sync_quiescent(home,source,credentials)


def test_account_id_alone_cannot_merge_different_provider_users(catalog):
    home, _, credentials=catalog
    write(credentials["b"],payload(user="another-user"))
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z"))
    assert observe(home,credentials).key=="a"
    incomplete=payload(); incomplete["tokens"].pop("id_token")
    write(home/"auth.json",incomplete)
    assert observe(home,credentials).status=="unmatched"


def test_duplicate_identity_and_revoked_account_fail_closed(catalog):
    home, _, credentials=catalog
    write(credentials["b"],payload())
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z"))
    assert observe(home,credentials).status=="conflict"
    (home/"auth.json").unlink(); (home/"auth.json").symlink_to(credentials["a"])
    credentials["a"].unlink()
    assert observe(home,{"b":credentials["b"]}).status=="revoked"


def test_concurrent_private_refreshes_never_restore_older_saved_tokens(catalog):
    home, source, credentials=catalog
    second=home.parent/"second"; second.mkdir()
    write(home/"auth.json",payload(refresh="2026-02-01T00:00:00Z",secret="middle"))
    write(second/"auth.json",payload(refresh="2026-03-01T00:00:00Z",secret="newest"))
    def sync(path):
        try: return sync_quiescent(path,source,credentials)
        except CredentialError: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(sync,[home,second]))
    assert read(credentials["a"]).digest==read(second/"auth.json").digest


@pytest.mark.parametrize("kind",["hardlink","symlink","world-writable"])
def test_unsafe_credential_storage_rejected(catalog,kind):
    home, _, credentials=catalog
    if kind=="hardlink": os.link(credentials["a"],home/"auth.json")
    elif kind=="symlink": (home/"auth.json").symlink_to(home.parent/"foreign.json")
    else:
        write(home/"auth.json",payload()); (home/"auth.json").chmod(0o666)
    assert observe(home,credentials).key is None


def test_runtime_receipt_tracks_incarnation_not_marker_or_pid_existence(catalog):
    home, _, credentials=catalog
    (home/"auth.json").symlink_to(credentials["a"])
    process=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],env={**os.environ,"CODEX_HOME":str(home)})
    try:
        activation_receipt(home,"scope-a","a",process.pid)
        digest=read(credentials["a"]).digest
        state=runtime_observation(home,"scope-a",digest)
        assert state["running_account_key"]=="a" and not state["running_restart_required"]
        assert runtime_observation(home,"wrong-scope",digest)["running_account_state"]=="unobserved"
        assert runtime_observation(home,"scope-a","changed")["running_restart_required"]
        receipt=json.loads((home/".termdeck-auth-runtime.json").read_text()); receipt["start"]+=1
        write(home/".termdeck-auth-runtime.json",receipt)
        assert runtime_observation(home,"scope-a",digest)["running_account_state"]=="stale-process"
        assert "fixture-token" not in json.dumps(state)
    finally:
        process.terminate(); process.wait(timeout=5)


def test_process_death_invalidates_receipt(catalog):
    home, _, credentials=catalog
    (home/"auth.json").symlink_to(credentials["a"])
    process=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],env={**os.environ,"CODEX_HOME":str(home)})
    activation_receipt(home,"scope-a","a",process.pid)
    process.terminate(); process.wait(timeout=5)
    assert runtime_observation(home,"scope-a",read(credentials["a"]).digest)["running_account_state"]=="stale-process"
