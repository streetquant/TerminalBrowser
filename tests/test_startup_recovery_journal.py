"""Headless recovery retains outcome evidence and supports guarded operator retry."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from termdeck import codex_config_scope as config, storage_fabric
from termdeck.recovery_coordinator import RecoveryCoordinator, RecoverySubmission
from termdeck.session_store import SessionStore
from termdeck.startup_recovery import (
    StartupRecoveryError, _input_digest, _load_attempts, recover_saved_terminals,
)
from termdeck.tmux_backend import TmuxBackend, TmuxError
from test_startup_recovery import Backend, record, run, store, _process_start


def _dead_attempt(tmp_path, item, state="pending", legacy=False):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    entry = {"digest": _input_digest(item), "state": state,
             "owner_pid": 999999999, "owner_start": "1", "owner_boot": "owned-test-boot"}
    payload = {"schema": "termdeck.startup-recovery-attempts.v2",
               "boot_id": "owned-test-boot", "attempts": {item.id: entry}}
    if legacy:
        payload.update(schema="termdeck.startup-recovery-attempts.v1",
                       attempts={item.id: entry["digest"]})
    path = runtime / "startup-recovery-attempts.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("state,legacy", [("pending", False), ("failed", False), ("ready", False), ("pending", True)])
def test_only_explicit_quiescent_retry_can_reconcile_absent_prior_owner(store, tmp_path, state, legacy):
    item = record(store, tmp_path)
    path = _dead_attempt(tmp_path, item, state, legacy)
    backend = Backend()
    normal = run(store, backend, tmp_path)
    assert normal["counts"] == {"blocked": 1}
    assert backend.created == []
    repaired = run(store, backend, tmp_path, session_ids={item.id}, retry_quiescent=True)
    assert repaired["counts"] == {"ready": 1}
    assert backend.created == [item.id]
    assert json.loads(path.read_text())["attempts"][item.id]["state"] == "ready"
    assert run(store, backend, tmp_path, session_ids={item.id}, retry_quiescent=True)["counts"] == {"blocked": 1}
    assert backend.created == [item.id]


def test_explicit_retry_rejects_live_prior_process_generation(store, tmp_path):
    item = record(store, tmp_path)
    path = _dead_attempt(tmp_path, item)
    payload = json.loads(path.read_text())
    payload["attempts"][item.id].update(owner_pid=os.getpid(), owner_start=_process_start(os.getpid()))
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    backend = Backend()
    result = run(store, backend, tmp_path, session_ids={item.id}, retry_quiescent=True)
    assert result["counts"] == {"blocked": 1}
    assert backend.created == []
    assert path.read_bytes() == before


def test_retry_requires_exact_selection_before_any_submission(store, tmp_path):
    record(store, tmp_path)
    backend = Backend()
    with pytest.raises(StartupRecoveryError, match="exact session selection"):
        run(store, backend, tmp_path, retry_quiescent=True)
    assert backend.created == []
    assert not (tmp_path / "runtime").exists()


def test_expired_live_native_claim_still_blocks_operator_retry(store, tmp_path):
    item = record(store, tmp_path)
    _dead_attempt(tmp_path, item)
    backend = Backend()
    coordinator = RecoveryCoordinator(backend, store)
    try:
        coordinator._claim_table(store.conn)
        key = coordinator._single_flight_keys(item)[0]
        store.conn.execute(
            "INSERT INTO recovery_claims(target_key, owner_id, claim_token, session_id, expires_at) VALUES(?,?,?,?,?)",
            (key, coordinator.owner_id, "owned-fixture-token", item.id, 0))
        store.conn.commit()
        result = run(store, backend, tmp_path, session_ids={item.id}, retry_quiescent=True)
        assert result["counts"] == {"blocked": 1}
        assert backend.created == []
    finally:
        coordinator.shutdown(wait=True)


def test_submission_exception_is_durable_and_does_not_block_healthy_tab(store, tmp_path, monkeypatch):
    bad = record(store, tmp_path, "bad")
    healthy = record(store, tmp_path, "healthy")
    original = RecoveryCoordinator.submit
    def submit(self, item, **kwargs):
        if item.id == bad.id:
            raise RuntimeError("private fixture launch text")
        return original(self, item, **kwargs)
    monkeypatch.setattr(RecoveryCoordinator, "submit", submit)
    backend = Backend()
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"failed": 1, "ready": 1}
    assert backend.created == [healthy.id]
    journal = (tmp_path / "runtime/startup-recovery-attempts.json").read_text()
    assert json.loads(journal)["attempts"][bad.id]["state"] == "failed"
    assert "private fixture launch text" not in journal + json.dumps(result)


def test_missing_callback_is_pending_and_cannot_report_all_ready(store, tmp_path, monkeypatch):
    item = record(store, tmp_path)
    monkeypatch.setattr(RecoveryCoordinator, "submit",
        lambda self, item, **kwargs: RecoverySubmission(item.id, "queued", True))
    started = time.monotonic()
    result = run(store, Backend(), tmp_path, overall_timeout=0.03)
    assert time.monotonic() - started < 0.8
    assert result["state"] == "incomplete"
    assert result["all_eligible_ready"] is False
    assert result["counts"] == {"pending": 1}
    assert json.loads((tmp_path / "runtime/startup-recovery-attempts.json").read_text())["attempts"][item.id]["state"] == "pending"


def test_stuck_owned_launch_does_not_defeat_worker_shutdown_deadline(store, tmp_path):
    record(store, tmp_path)
    backend = Backend()
    entered = threading.Event()
    release = threading.Event()
    def stuck(item):
        entered.set()
        release.wait(3)
        backend.live[item.id] = 7400
    backend.create_or_resume = stuck
    started = time.monotonic()
    try:
        result = run(store, backend, tmp_path, overall_timeout=0.05)
        assert entered.is_set()
        assert time.monotonic() - started < 1.0
        assert result["state"] == "incomplete"
        assert result["worker_quiescent"] is False
        assert result["all_eligible_ready"] is False
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while any(t.name.startswith("termdeck-recovery") and t.is_alive()
                  for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.mark.parametrize("malformed", [[], None, {"schema": "wrong", "attempts": {}},
                                     {"schema": "termdeck.startup-recovery-attempts.v2", "attempts": {"id": []}}])
def test_malformed_attempt_journal_fails_closed(tmp_path, malformed):
    path = tmp_path / "journal"
    path.write_text(json.dumps(malformed))
    path.chmod(0o600)
    with pytest.raises(StartupRecoveryError):
        _load_attempts(path, "owned-test-boot")


def test_explicit_deferred_native_account_is_not_launched(store, tmp_path):
    item = store.create("owned-native", "work", "codex-opencode resume 00000000-0000-0000-0000-000000000001", str(tmp_path))
    item = store.update(item.id, status="running", selected_account="iisc")
    backend = Backend()
    result = run(store, backend, tmp_path, deferred_native_accounts={"iisc"})
    assert result["counts"] == {"blocked": 1}
    assert result["sessions"][0]["reason"] == "native-account-work-deferred"
    assert backend.created == []


@pytest.mark.parametrize("settles,grace,expected", [
    (True, 0.2, "ready"), (False, 0.2, "failed"), (True, 0, "failed"),
])
def test_idle_shell_bootstrap_grace_is_bounded_and_gui_default_unchanged(settles, grace, expected):
    now = [0.0]
    class Readiness:
        def recovery_readiness(self, _record):
            if settles and now[0] >= 0.1:
                return {"state": "ready", "pid": 7100}
            return {"state": "failed", "pid": 7100,
                    "detail": "managed Codex launcher exited to an idle shell"}
        def authoritative_live_pane_pid(self, _record): return 7100
        def session_exists(self, _record): return True
    coordinator = RecoveryCoordinator(Readiness(), readiness_timeout=1, readiness_failure_grace=grace,
        clock=lambda: now[0], sleeper=lambda dt: now.__setitem__(0, now[0] + dt))
    item = SimpleNamespace(command="codex", recovery_command=None)
    coordinator._payload_ready = lambda record, pid: False
    try:
        if expected == "ready":
            assert coordinator._await_ready(item) == 7100
        else:
            with pytest.raises(TmuxError, match="idle shell"):
                coordinator._await_ready(item)
        assert now[0] <= max(grace, 0.1) + 0.05
    finally:
        coordinator.shutdown(wait=True)


@pytest.mark.parametrize("retired", [False, True])
def test_real_headless_historical_bash_scope_preserves_home_and_skips_codex(tmp_path, monkeypatch, retired):
    root = tmp_path / "tm"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    for name in ("TMUX", "TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "CODEX_HOME",
                 "CODEX_AUTH_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_SQLITE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_TMUX_RUNTIME_BINDING", None)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", None)
    user = tmp_path / "user"
    user.mkdir(mode=0o700)
    (user / ".bashrc").write_text("export TERMDECK_OWNED_RC=loaded\n")
    monkeypatch.setenv("HOME", str(user))
    source = tmp_path / "canonical"
    if not retired:
        source.mkdir(mode=0o700)
        (source / "config.toml").write_text("model='synthetic'\n")
        (source / "pets").mkdir(mode=0o700)  # New asset absent from this historical scope.
    historical = tmp_path / "historical"
    historical.mkdir(mode=0o700)
    home = historical / "auth-saved"
    home.mkdir(mode=0o700)
    (home / config._CANONICAL_HOME_BINDING).symlink_to(source)
    (home / "config.toml").symlink_to(source / "config.toml")
    (home / "bin").mkdir(mode=0o700)
    wrapper = home / "bin/codex"
    wrapper.write_text(config._wrapper_content(home, tmp_path / "absent-native-codex"))
    wrapper.chmod(0o700)
    private = home / "auth.json"
    private.write_bytes(b"owned-synthetic-auth-preimage")
    before = hashlib.sha256(private.read_bytes()).hexdigest()
    value = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    item = value.create("owned-historical-bash", "shell", "/bin/bash -i", str(tmp_path))
    # Construct a pre-existing historical binding; ordinary update correctly
    # keeps its auth_scope_id immutable. This affects only this synthetic DB.
    value.conn.execute("UPDATE sessions SET status=\'running\', recovery_intent='active', auth_scope_id=?, codex_home=? WHERE id=?",
                       (home.name, str(home), item.id))
    value.conn.commit()
    item = value.get(item.id)
    backend = TmuxBackend(executable="/usr/bin/tmux")
    server_pid = server_start = None
    monkeypatch.setattr(config, "resolve_codex_launcher",
                        lambda *args, **kwargs: pytest.fail("direct Bash must not qualify native Codex"))
    try:
        result = recover_saved_terminals(value, backend, runtime_root=tmp_path / "runtime",
                                        readiness_timeout=2, boot_id="owned-test-boot")
        assert result["counts"] == {"ready": 1}
        assert result["all_eligible_ready"] is True
        evidence = backend.reconciliation_snapshot([item])[item.id]
        assert evidence["state"] == "live"
        server_pid, server_start = evidence["server_pid"], _process_start(evidence["server_pid"])
        assert value.get(item.id).codex_home == str(home)
        assert value.get(item.id).auth_scope_id == home.name
        assert hashlib.sha256(private.read_bytes()).hexdigest() == before
        assert not (home / "pets").exists()
        assert not (home / "state_5.sqlite").exists()
        assert not value.auth_root.exists()
        assert run(value, backend, tmp_path)["counts"] == {"already-live": 1}
    finally:
        value.close()
        socket = root / f"tmux-{os.geteuid()}" / "default"
        if server_pid is not None and _process_start(server_pid) == server_start:
            subprocess.run(["/usr/bin/tmux", "-S", str(socket), "kill-server"],
                           capture_output=True, timeout=3, check=False)
            deadline = time.monotonic() + 3
            while _process_start(server_pid) == server_start and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _process_start(server_pid) != server_start



def test_cli_exception_after_worker_start_exits_without_executor_atexit_hang(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys, time; from concurrent.futures import ThreadPoolExecutor; "
        + "sys.path.insert(0, " + repr(str(root)) + "); "
        + "import termdeck.startup_recovery as recovery; "
        + "pool = ThreadPoolExecutor(max_workers=1); "
        + "pool.submit(time.sleep, 30); "
        + "recovery.run_managed_recovery = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('private-fixture-text')); "
        + "raise SystemExit(recovery.main(['--managed-root', " + repr(str(tmp_path)) + "]))"
    )
    started = time.monotonic()
    child = subprocess.run(["/usr/bin/python", "-B", "-I", "-c", code],
                           capture_output=True, timeout=3)
    assert time.monotonic() - started < 2
    assert child.returncode == 1
    assert json.loads(child.stdout)["error_type"] == "RuntimeError"
    assert b"private-fixture-text" not in child.stdout + child.stderr


@pytest.mark.parametrize("result,expected_exit", [
    ({"state": "complete", "all_eligible_ready": True, "counts": {}}, 0),
    ({"state": "complete", "all_eligible_ready": True,
      "counts": {"ready": 2, "already-live": 1, "skipped": 1}}, 0),
    ({"state": "complete", "all_eligible_ready": False,
      "counts": {"ready": 1, "blocked": 1}}, 1),
    ({"state": "complete", "all_eligible_ready": False,
      "counts": {"failed": 1}}, 1),
    ({"state": "incomplete", "all_eligible_ready": False,
      "counts": {"pending": 1}}, 1),
    ({"state": "incomplete", "all_eligible_ready": False,
      "journal_durable": False, "counts": {"ready": 1}}, 1),
    ({"state": "already-owned", "all_eligible_ready": False, "sessions": []}, 1),
])
def test_cli_exit_reports_aggregate_recovery_completion(tmp_path, result, expected_exit):
    """An exited oneshot must not appear successful with unrecovered tabs."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        + "sys.path.insert(0, " + repr(str(root)) + "); "
        + "import termdeck.startup_recovery as recovery; "
        + "recovery.run_managed_recovery = lambda *a, **k: " + repr(result) + "; "
        + "raise SystemExit(recovery.main(['--managed-root', " + repr(str(tmp_path)) + "]))"
    )
    child = subprocess.run(["/usr/bin/python", "-B", "-I", "-c", code],
                           capture_output=True, timeout=3)
    assert child.returncode == expected_exit
    assert json.loads(child.stdout) == result
    assert child.stderr == b""
