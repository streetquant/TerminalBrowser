import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from termdeck.codex_credentials import CredentialError, activation_receipt, observe, process_start, read, runtime_observation, sync_quiescent


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


def test_newer_legacy_private_link_synchronizes_without_replacing_the_link(catalog):
    home, source, credentials = catalog
    accounts = home / "accounts"; accounts.mkdir(mode=0o700)
    private = accounts / "a.json"
    write(private, payload(refresh="2026-02-01T00:00:00Z", secret="newer-fixture"))
    (home / "auth.json").symlink_to("accounts/a.json")
    other = credentials["b"].read_bytes()

    assert observe(home, credentials).status == "refreshed"
    assert sync_quiescent(home, source, credentials) == "synchronized"
    assert credentials["a"].read_bytes() == private.read_bytes()
    assert (home / "auth.json").is_symlink()
    assert os.readlink(home / "auth.json") == "accounts/a.json"
    assert credentials["b"].read_bytes() == other


def test_legacy_link_retargeted_before_publication_does_not_update_catalogue(catalog, monkeypatch):
    from termdeck import codex_credentials
    home, source, credentials = catalog
    accounts = home / "accounts"; accounts.mkdir(mode=0o700)
    private = accounts / "a.json"
    newer = payload(refresh="2026-02-01T00:00:00Z", secret="newer-fixture")
    write(private, newer)
    (home / "auth.json").symlink_to("accounts/a.json")
    foreign = home.parent / "foreign.json"; write(foreign, newer)
    before = credentials["a"].read_bytes()
    mkstemp = codex_credentials.tempfile.mkstemp

    def retarget(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        (home / "auth.json").unlink()
        (home / "auth.json").symlink_to(foreign)
        return result

    monkeypatch.setattr(codex_credentials.tempfile, "mkstemp", retarget)
    with pytest.raises(CredentialError, match="changed before publication"):
        sync_quiescent(home, source, credentials)
    assert credentials["a"].read_bytes() == before
    assert private.read_bytes() == foreign.read_bytes()
    assert not list(credentials["a"].parent.glob(".termdeck-refresh-*"))


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


def test_runtime_receipt_tracks_incarnation_not_marker_or_pid_existence(catalog, monkeypatch):
    home, _, credentials=catalog
    (home/"auth.json").symlink_to(credentials["a"])
    process=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],env={**os.environ,"CODEX_HOME":str(home)})
    try:
        digest=read(credentials["a"]).digest
        # The fixture process supplies a real incarnation; the guarded backend's
        # Codex classification is simulated without running a provider process.
        monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ((process.pid, process_start(process.pid)),))
        activation_receipt(home,"scope-a","a",process.pid,kind="codex-restart",expected_digest=digest)
        state=runtime_observation(home,"scope-a",digest)
        assert state["running_account_key"]=="a" and not state["running_restart_required"]
        assert state["running_account_state"] == "restart-observed"
        assert runtime_observation(home,"wrong-scope",digest)["running_account_state"]=="unobserved"
        assert runtime_observation(home,"scope-a","changed")["running_restart_required"]
        assert runtime_observation(home,"scope-a",digest,record_status="running",record_pid=process.pid+1)["running_account_state"] == "stale-process"
        receipt=json.loads((home/".termdeck-auth-runtime.json").read_text()); receipt["start"]+=1
        write(home/".termdeck-auth-runtime.json",receipt)
        assert runtime_observation(home,"scope-a",digest)["running_account_state"]=="stale-process"
        assert "fixture-token" not in json.dumps(state)
    finally:
        process.terminate(); process.wait(timeout=5)


def test_process_death_invalidates_receipt(catalog, monkeypatch):
    home, _, credentials=catalog
    (home/"auth.json").symlink_to(credentials["a"])
    process=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],env={**os.environ,"CODEX_HOME":str(home)})
    monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ((process.pid, process_start(process.pid)),))
    activation_receipt(home,"scope-a","a",process.pid,kind="codex-restart",expected_digest=read(credentials["a"]).digest)
    process.terminate(); process.wait(timeout=5)
    assert runtime_observation(home,"scope-a",read(credentials["a"]).digest)["running_account_state"]=="stale-process"


def test_idle_shell_receipt_never_claims_a_codex_restart(catalog, monkeypatch):
    home, _, credentials = catalog
    (home / "auth.json").symlink_to(credentials["a"])
    digest = read(credentials["a"]).digest
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env={**os.environ, "CODEX_HOME": str(home)})
    try:
        activation_receipt(home, "scope-a", "a", process.pid, kind="idle-shell", expected_digest=digest)
        state = runtime_observation(home, "scope-a", digest)
        assert state["running_account_state"] == "no-codex"
        assert state["running_account_key"] is None
        assert not state["running_restart_required"]
        # A later Codex child has no restart proof from the idle shell receipt.
        monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ((process.pid, process_start(process.pid)),))
        later = runtime_observation(home, "scope-a", digest)
        assert later["running_account_state"] == "unobserved"
        assert later["running_account_key"] is None
        assert later["running_restart_required"]
    finally:
        process.terminate(); process.wait(timeout=5)


def test_restarted_codex_receipt_expires_when_its_child_is_replaced(catalog, monkeypatch):
    home, _, credentials = catalog
    (home / "auth.json").symlink_to(credentials["a"])
    digest = read(credentials["a"]).digest
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env={**os.environ, "CODEX_HOME": str(home)})
    try:
        # A real root process survives each simulated child lifecycle.
        monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ((12345, 100),))
        activation_receipt(home, "scope-a", "a", process.pid, kind="codex-restart", expected_digest=digest)
        assert runtime_observation(home, "scope-a", digest)["running_account_state"] == "restart-observed"
        monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ())
        idle = runtime_observation(home, "scope-a", digest)
        assert idle["running_account_state"] == "no-codex"
        assert idle["running_account_key"] is None
        monkeypatch.setattr("termdeck.codex_credentials._codex_processes", lambda _: ((12345, 101),))
        later = runtime_observation(home, "scope-a", digest)
        assert later["running_account_state"] == "unobserved"
        assert later["running_account_key"] is None
        assert later["running_restart_required"]
    finally:
        process.terminate(); process.wait(timeout=5)


def test_stopped_receipt_requires_a_current_stopped_record(catalog):
    home, _, credentials = catalog
    (home / "auth.json").symlink_to(credentials["a"])
    digest = read(credentials["a"]).digest
    activation_receipt(home, "scope-a", "a", None, kind="stopped", expected_digest=digest)
    assert runtime_observation(home, "scope-a", digest)["running_account_state"] == "unobserved"
    assert runtime_observation(home, "scope-a", digest, record_status="stopped")["running_account_state"] == "stopped"
    for status, pid in [("new", None), ("running", 12345), ("stopped", 12345)]:
        state = runtime_observation(home, "scope-a", digest, record_status=status, record_pid=pid)
        assert state["running_account_state"] == "unobserved"
        assert state["running_account_key"] is None
        assert state["running_restart_required"]


def test_untyped_legacy_receipt_cannot_claim_a_restart(catalog):
    home, _, credentials = catalog
    write(home / ".termdeck-auth-runtime.json", {"version": 1, "scope": "scope-a", "pid": None})
    state = runtime_observation(home, "scope-a", read(credentials["a"]).digest, record_status="stopped")
    assert state["running_account_state"] == "unobserved"


def test_refresh_during_activation_cannot_be_adopted_as_restart_evidence(catalog):
    home, _, credentials = catalog
    (home / "auth.json").symlink_to(credentials["a"])
    selected_digest = read(credentials["a"]).digest
    write(credentials["a"], payload(refresh="2026-02-01T00:00:00Z", secret="newer-fixture"))
    with pytest.raises(CredentialError, match="changed during account activation"):
        activation_receipt(home, "scope-a", "a", None, kind="stopped", expected_digest=selected_digest)
    assert not (home / ".termdeck-auth-runtime.json").exists()
