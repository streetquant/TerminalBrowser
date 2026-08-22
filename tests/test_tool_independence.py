from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend


def test_default_store_never_provisions_optional_agent_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        database = root / "sessions.sqlite3"
        store = SessionStore(database)
        try:
            record = store.create(
                "worker",
                "generic",
                "/usr/bin/python3 -c 'import time; time.sleep(60)'",
                cwd=tmp,
            )
            assert record.auth_scope_id is None
            assert record.codex_home is None
            assert record.recovery_recipe
            assert record.recovery_recipe_sha256
            assert not (root / "terminal-auth").exists()
            assert not (root / ".codex").exists()
        finally:
            store.close()


def test_generic_backend_never_consults_agent_native_recognizers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.sqlite3")
        try:
            record = store.create(
                "worker",
                "generic",
                "/usr/bin/python3 -c 'import time; time.sleep(60)'",
                cwd=tmp,
            )
        finally:
            store.close()
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(["tmux"], 0, "", "")
        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend,
            "_exact_codex_recovery",
            side_effect=AssertionError("generic recovery consulted optional native syntax"),
        ), mock.patch.object(
            backend,
            "_exact_claude_recovery",
            side_effect=AssertionError("generic recovery consulted optional native syntax"),
        ), mock.patch.object(
            backend.checkpoints,
            "restart_command",
            return_value=None,
        ), mock.patch.object(
            backend.checkpoints,
            "wrap_launch_command",
            side_effect=lambda _record, command: command,
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)
        launch_command = run.call_args_list[0].args[0][-1]
        assert record.command in launch_command
        assert "__termdeck_status=$?" in launch_command
        assert "interactive shell ready" in launch_command


def test_core_registry_and_recipe_work_with_empty_executable_path() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        empty_path = root / "empty-bin"
        home.mkdir()
        empty_path.mkdir()
        script = r'''
import json
import sys
from pathlib import Path

repository = Path(sys.argv[1])
root = Path(sys.argv[2])
sys.path.insert(0, str(repository))

from termdeck.session_recipe import parse_session_recipe
from termdeck.session_reconcile import reconcile_records
from termdeck.session_store import SessionStore

class EmptyBackend:
    def session_exists(self, _record):
        return False

    def primary_pid(self, _record):
        return None

store = SessionStore(root / "sessions.sqlite3")
try:
    record = store.create(
        "generic-service",
        "work",
        "/usr/bin/python3 -c 'import time; time.sleep(60)'",
        cwd=root,
        environment={"MODE": "test"},
        restart_policy="on-failure",
    )
    assert record.auth_scope_id is None
    assert record.codex_home is None
    recipe = parse_session_recipe(
        record.recovery_recipe,
        record.recovery_recipe_sha256,
    )
    assert recipe.command == record.command
    assert recipe.environment_dict() == {"MODE": "test"}
    store.update(record.id, status="running")
    report = reconcile_records(
        store.list(),
        store=store,
        backend=EmptyBackend(),
        recover_missing=True,
    )
    recovered = store.get(record.id)
    assert recovered is not None
    assert recovered.status == "recovery-pending"
    assert sum(row.after_status == "recovery-pending" for row in report.rows) == 1
    assert not (root / "terminal-auth").exists()
    print(json.dumps({
        "ok": True,
        "status": recovered.status,
        "auth_scope_id": recovered.auth_scope_id,
        "codex_home": recovered.codex_home,
    }, sort_keys=True))
finally:
    store.close()
'''
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": str(empty_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(repository), str(root)],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=True,
        )
        payload = json.loads(completed.stdout)
        assert payload["ok"] is True
        assert payload["status"] == "recovery-pending"
        assert not (root / "terminal-auth").exists()
        assert not (home / ".codex").exists()
