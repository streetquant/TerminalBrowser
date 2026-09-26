"""Configured task account handoff with a local compiled, provider-free runtime."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import sqlite3
import subprocess
import time
from types import SimpleNamespace
import uuid

import pytest

from termdeck import agent_launch as launch, client_files as cf, resources
from termdeck.codex_auth import CodexAuthManager
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend, TmuxError, _AuthPaneIdentity, auth_transaction_lock, execute_auth_change
from termdeck.web_accounts import handle
from test_agent_launch import Backend, configured, request as launch_request  # noqa: F401
from test_client_files import stage, upload, path, ordinary_workspace_root  # noqa: F401
from test_codex_auth import _write_native_source, _bind_native_source_default


CONVERSATION = "01234567-89ab-4cde-8123-456789abcdef"
ROOT = Path(__file__).resolve().parents[1]
NATIVE_FIXTURE = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char filename[PATH_MAX], cwd[PATH_MAX], auth[4096] = {0}, bytes[128] = {0};
    const char *home = getenv("CODEX_HOME"), *id = CONVERSATION;
    int resumed = 0;
    if (!home || !getcwd(cwd, sizeof(cwd))) return 30;
    for (int i = 1; i + 1 < argc; ++i)
        if (!strcmp(argv[i], "resume")) { id = argv[i+1]; resumed = 1; }
    snprintf(filename, sizeof(filename), "%s/auth.json", home);
    FILE *credential = fopen(filename, "r");
    if (!credential) return 31;
    fread(auth, 1, sizeof(auth)-1, credential); fclose(credential);
    const char *account = strstr(auth, "alpha-refresh") ? "alpha" : strstr(auth, "beta-refresh") ? "beta" : "unknown";
    FILE *attachment = fopen(ATTACHMENT, "r");
    if (!attachment) return 32;
    fread(bytes, 1, sizeof(bytes)-1, attachment); fclose(attachment);
    if (access(SENTINEL, F_OK) == 0) return 33;
    if (fopen(ATTACHMENT, "a")) return 34;
    snprintf(filename, sizeof(filename), "%s/sessions", home); mkdir(filename, 0700);
    snprintf(filename, sizeof(filename), "%s/sessions/rollout-%s.jsonl", home, id);
    FILE *rollout = fopen(filename, "a+");
    if (!rollout) return 35;
    fprintf(rollout, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"%s\",\"cwd\":\"%s\",\"source\":\"cli\"}}\n", id, cwd);
    fflush(rollout);
    FILE *proof = fopen("observations.jsonl", "a");
    if (!proof) return 36;
    fprintf(proof, "{\"account\":\"%s\",\"conversation\":\"%s\",\"cwd\":\"%s\",\"bytes\":\"%s\",\"resumed\":%d}\n", account, id, cwd, bytes, resumed);
    fclose(proof);
    for (;;) pause();
}
'''


class ProcessPane(TmuxBackend):
    """Replace only tmux transport; retain real /proc, gates and auth lifecycle."""
    def __init__(self, workspace, environment):
        super().__init__()
        self.workspace, self.environment = workspace, environment
        self.process = None
        self.input_off = False
        self.launched_specs = []

    def launch(self, command, environment=()):
        self.process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=self.workspace, env={**self.environment, **dict(environment)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def close(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)

    def _auth_session_exists(self, _record):
        return self.process is not None and self.process.poll() is None

    def _inspect_auth_pane(self, record):
        if not self._auth_session_exists(record):
            return None
        return _AuthPaneIdentity("disposable", "%1", self.process.pid, "codex", str(self.workspace))

    def _disable_auth_pane_input(self, _pane):
        before = self.input_off
        self.input_off = True
        return before

    def _restore_auth_pane_input(self, _pane, before):
        self.input_off = before

    def _respawn_auth_pane_gated(self, record, plan, spec, gate_path):
        self.close()
        self.launched_specs.append(spec)
        self.launch(self._gated_auth_launch_command(spec, gate_path), spec.environment)

    def _run(self, *_args, **_kwargs):
        raise AssertionError("fixture cannot use a real tmux server")


def wait_for(predicate, backend):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        assert backend.process.poll() is None, "disposable configured runtime exited"
        time.sleep(0.02)
    pytest.fail("disposable configured runtime did not become ready")


@pytest.fixture(params=["saved", "server-directory"])
def confined(configured, monkeypatch, request):
    store, actor, workspace, config = configured
    if request.param == "server-directory":
        workspace = workspace.with_name(workspace.name + " unregistered with spaces")
        workspace.mkdir(mode=0o770)
    # No discovery of the developer's native launcher or credentials.
    isolated_home = store.root.parent / "isolated-home"
    isolated_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher", lambda *_a, **_k: None)
    monkeypatch.setattr("termdeck.codex_auth._real_codex_auth", lambda *_a, **_k: None)
    source = store.root.parent / "catalogue"
    _write_native_source(source)
    _bind_native_source_default(source)
    source.chmod(0o700)
    (source / "sessions").mkdir(mode=0o700)
    (source / "sessions" / "standalone-history").write_bytes(b"preserve standalone history")
    obj = upload((store, actor), b"original")
    attachment = path((store, actor), obj)
    sentinel = store.root.parent / "host-secret"
    sentinel.write_text("must remain outside the task")
    runtime = store.root.parent / "runtime"
    runtime.mkdir(mode=0o700)
    executable = runtime / "codex"
    compiled = subprocess.run(
        ["/usr/bin/cc", "-x", "c", "-o", str(executable), "-",
         "-DCONVERSATION=" + json.dumps(CONVERSATION),
         "-DATTACHMENT=" + json.dumps(str(attachment)),
         "-DSENTINEL=" + json.dumps(str(sentinel))],
        input=NATIVE_FIXTURE, text=True, capture_output=True, timeout=15,
    )
    assert compiled.returncode == 0, compiled.stderr
    config["codex"] = {"executable": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "account_source": str(source)}
    (store.root.parent / launch.POLICY).write_text(json.dumps(config))
    creation = launch_request(configured, runtime="codex", task="--Review the immutable files", attachments=[obj["reference"]])
    if request.param == "server-directory":
        creation.pop("workspace_id")
        creation["workspace_path"] = str(workspace)
    receipt = store.handle(actor, creation)
    assert launch.start(store, actor, receipt, backend=Backend())["status"] == "started"
    sessions = SessionStore(store.session_database, account_source=source, provision_auth_scopes=False)
    record, sibling = [sessions.ensure_auth_scope(identity, private_history=True) for identity in receipt["tabs"]]
    manager = CodexAuthManager(account_source=source)
    environment = {"HOME": str(isolated_home), "PATH": "/usr/bin", "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    backend = ProcessPane(workspace, environment)
    backend.launch(record.command)
    try:
        wait_for(lambda: (workspace / "observations.jsonl").exists(), backend)
        record = sessions.update(record.id, status="running", last_pid=backend.process.pid)
        yield SimpleNamespace(store=store, actor=actor, sessions=sessions, record=record, sibling=sibling,
                              manager=manager, backend=backend, source=source, workspace=workspace,
                              config=config, receipt=receipt, obj=obj, attachment=attachment)
    finally:
        backend.close()
        sessions.close()


def observe(fixture):
    return launch.observe_resume(fixture.record, fixture.backend.process.pid)


def resume_record(fixture):
    observed, recovery, cwd = observe(fixture)
    assert observed and recovery
    return replace(fixture.record, recovery_command=recovery, recovery_cwd=cwd)


def args_for(fixture, permit):
    return SimpleNamespace(state=str(fixture.store.root.parent), auth_dir=str(fixture.store.auth_directory),
                           db=str(fixture.store.session_database), launch=fixture.receipt["launch_id"],
                           tab=fixture.record.id, resume_permit=permit)


def test_configured_task_switch_uses_real_confined_argv_and_exact_guarded_resume(confined):
    f = confined
    record = resume_record(f)
    initial_pid = f.backend.process.pid
    assert resources.observe_recovery_metadata(initial_pid) == (True, None, None)
    source_before = {p.relative_to(f.source): p.read_bytes() for p in f.source.rglob("*") if p.is_file()}
    sibling_before = (Path(f.sibling.codex_home) / "auth.json").read_bytes()
    upload((f.store, f.actor), b"new generation", previous=f.obj["reference"])
    request = {"operation": "switch-account", "ids": [record.id], "confirmation": [record.id],
               "account": "beta", "expected": {record.id: f.manager.state(record).fingerprint},
               "request_id": uuid.uuid4().hex, "_actor_session": f.actor.session_id}
    result = handle(f.sessions, request, manager=f.manager, backend=f.backend)
    assert result["ok"], result
    row = result["results"][0]
    assert row["restarted"] and row["state"]["running_account_key"] == "beta"
    assert row["state"]["running_account_state"] == "restart-observed"
    assert f.backend.process.pid != initial_pid and not f.backend.input_off
    assert handle(f.sessions, request, manager=f.manager, backend=f.backend) == result
    assert len(f.backend.launched_specs) == 1
    spec = f.backend.launched_specs[0]
    assert "termdeck.agent_launch" in spec.command and "--resume-permit" in spec.command
    assert not spec.checkpoint and spec.application_native_resume
    persisted = f.sessions.get(record.id)
    assert persisted.command == record.command
    assert persisted.recovery_command == record.recovery_command
    proofs = [json.loads(line) for line in (f.workspace / "observations.jsonl").read_text().splitlines()]
    assert [(p["account"], p["resumed"]) for p in proofs] == [("alpha", 0), ("beta", 1)]
    assert all(p["conversation"] == CONVERSATION and p["cwd"] == str(f.workspace) and p["bytes"] == "original" for p in proofs)
    assert f.attachment.read_bytes() == b"original"
    assert (Path(f.sibling.codex_home) / "auth.json").read_bytes() == sibling_before
    assert {p.relative_to(f.source): p.read_bytes() for p in f.source.rglob("*") if p.is_file()} == source_before
    assert not (Path(record.codex_home) / "sessions").is_symlink()
    assert "alpha-refresh" not in json.dumps(result) and "beta-refresh" not in json.dumps(result)
    with pytest.raises(cf.StagingError, match="consumed"):
        launch.execute(args_for(f, spec.confined_resume_permit))


@pytest.mark.parametrize("mutation,detail", [
    ("policy", "policy changed"), ("machine", "machine was revoked"),
    ("expired-login", "login expired"), ("revoked-login", "login was revoked"),
    ("tab", "binding changed"), ("attachment", "no longer authorized"),
    ("workspace", "directory identity changed"), ("launcher", "launcher identity changed"),
])
def test_resume_revalidates_original_authority_before_prepare_and_execute(confined, mutation, detail):
    f = confined
    record = resume_record(f)
    _command, permit = launch.prepare_resume(record)
    if mutation == "policy":
        f.config["workspaces"][0]["label"] = "new revision"
        (f.store.root.parent / launch.POLICY).write_text(json.dumps(f.config))
    elif mutation == "tab":
        f.sessions.update(record.id, cwd=str(f.store.root.parent))
    elif mutation == "workspace":
        f.workspace.rename(f.workspace.with_name(f.workspace.name + "-original"))
        f.workspace.mkdir()
    elif mutation == "launcher":
        words = shlex.split(record.command)
        words[1] = "/usr/bin/false"
        f.sessions.update(record.id, command=shlex.join(words))
    else:
        database, statement = {
            "machine": (f.store.auth_directory / "termdeck-machines-v1.sqlite3", "UPDATE machines SET revoked_at_ms=1"),
            "expired-login": (f.store.auth_directory / "termdeck-web-auth-v1.sqlite3", "UPDATE auth_sessions SET expires_at_ms=1"),
            "revoked-login": (f.store.auth_directory / "termdeck-web-auth-v1.sqlite3", "UPDATE auth_sessions SET revoked_at_ms=1"),
            "attachment": (f.store.root / "metadata.sqlite3", "UPDATE attachments SET target='window:other'"),
        }[mutation]
        with sqlite3.connect(database) as db:
            db.execute(statement)
    before = (Path(record.codex_home) / "auth.json").read_bytes()
    with pytest.raises(cf.StagingError, match=detail):
        launch.prepare_resume(record)
    with pytest.raises(cf.StagingError, match=detail):
        launch.execute(args_for(f, permit))
    plan = f.backend.plan_auth_change(record)
    assert not plan.ok and detail in plan.detail
    assert not f.backend.launched_specs and f.backend.process.poll() is None
    assert (Path(record.codex_home) / "auth.json").read_bytes() == before


def test_resume_permits_are_single_use_superseded_and_cancelled(confined, monkeypatch):
    f = confined
    record = resume_record(f)
    _, superseded = launch.prepare_resume(record)
    _, cancelled = launch.prepare_resume(record)
    with pytest.raises(cf.StagingError, match="consumed"):
        launch.execute(args_for(f, superseded))
    launch.cancel_resume(record, cancelled)
    with pytest.raises(cf.StagingError, match="consumed"):
        launch.execute(args_for(f, cancelled))
    _, active = launch.prepare_resume(record)
    commands = []
    class Executed(Exception):
        pass
    def execute(executable, argv, environment):
        commands.append((executable, argv, environment))
        raise Executed()
    monkeypatch.setattr(os, "execve", execute)
    with pytest.raises(Executed):
        launch.execute(args_for(f, active))
    with pytest.raises(cf.StagingError, match="consumed"):
        launch.execute(args_for(f, active))
    executable, argv, environment = commands[0]
    assert executable == "/usr/bin/bwrap"
    assert argv[-2:] == ["resume", CONVERSATION]
    assert f.config["codex"]["executable"] in argv
    assert environment["TERMDECK_WEB_LAUNCH_ID"] == f.receipt["launch_id"]
    assert len(commands) == 1


def test_abandoned_handoff_cancels_its_permit_and_preserves_running_account(confined, monkeypatch):
    f = confined
    old_pid = f.backend.process.pid
    def refuse(*_args):
        raise TmuxError("Disposable transport cannot replace its pane")
    monkeypatch.setattr(f.backend, "_respawn_auth_pane_gated", refuse)
    before = f.manager.state(f.record)
    with auth_transaction_lock(f.record):
        result = execute_auth_change(f.backend, f.manager, f.record, before, "beta")
    assert not result.ok and "cannot replace" in result.error
    assert f.backend.process.pid == old_pid and f.backend.process.poll() is None
    assert not f.backend.input_off
    assert f.manager.state(f.record).selected_account_key == "alpha"
    with sqlite3.connect(f.store.root / "metadata.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM launch_resumes WHERE used=0").fetchone()[0] == 0


def test_configured_observer_refuses_unmatched_process_without_changing_generic_parser(confined, monkeypatch):
    f = confined
    assert observe(f)[1]
    original = resources._process_argv
    monkeypatch.setattr(resources, "_process_argv", lambda pid: (*original(pid), "unapproved") if original(pid) else None)
    assert observe(f) == (True, None, None)
    assert resources.safe_codex_recovery_command(("codex", "resume", CONVERSATION)) == f"codex resume {CONVERSATION}"
    assert resources.safe_codex_recovery_command(("codex", "--ask-for-approval", "on-request", "task")) is None
