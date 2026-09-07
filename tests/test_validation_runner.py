from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "run_validation.py"
MANIFEST = REPO_ROOT / "scripts" / "validation_manifest.json"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


def _load_runner():
    spec = importlib.util.spec_from_file_location("termdeck_validation_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _write_canonical(path: Path, payload: object) -> None:
    path.write_bytes(runner.canonical_json_bytes(payload))


def _fixture_manifest(smoke_path: str = "scripts/check_demo_smoke.py") -> dict[str, object]:
    return {
        "limits": {
            "smoke_json_bytes": 4096,
            "stderr_bytes": 4096,
            "stdout_bytes": 4096,
            "termination_grace_seconds": 1,
        },
        "profiles": ["ci", "production"],
        "schema": "t1000.validation-manifest.v1",
        "smokes": [
            {
                "environment": "xvfb-dbus",
                "path": smoke_path,
                "profiles": ["ci", "production"],
                "timeout_seconds": 5,
            }
        ],
        "tests": {
            "pattern": "tests/test_*.py",
            "profiles": ["ci", "production"],
            "timeout_seconds": 5,
        },
    }


def _fixture_repo(tmp_path: Path, *, smoke_source: str = "print('not-json')\n") -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts" / "check_demo_smoke.py").write_text(smoke_source, encoding="utf-8")
    (root / "tests" / "test_dummy.py").write_text("def test_dummy():\n    assert True\n", encoding="utf-8")
    _write_canonical(root / "scripts" / "validation_manifest.json", _fixture_manifest())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def test_validation_contract_exists_and_ci_has_no_manual_inventory() -> None:
    assert RUNNER.is_file()
    assert MANIFEST.is_file()
    ci = CI.read_text(encoding="utf-8")
    assert "scripts/run_validation.py" in ci
    assert re.search(r"scripts/check_[a-z0-9_]+_smoke\.py", ci) is None
    assert re.search(r"(?:python|pytest)[^\n]*pytest[^\n]*\btests\b", ci) is None
    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    assert "scripts/run_validation.py" in contributing
    assert "python3 -m pytest tests" not in contributing


def test_actual_manifest_is_canonical_and_covers_exact_inventory() -> None:
    payload, raw, tests, smokes = runner.load_manifest(REPO_ROOT, MANIFEST)
    assert raw == runner.canonical_json_bytes(payload)
    assert len(tests) >= 100
    assert len(smokes) == 25
    entries = {entry["path"]: entry for entry in payload["smokes"]}
    assert sorted(entries) == smokes
    managed = entries["scripts/check_managed_process_inventory_smoke.py"]
    assert managed["profiles"] == ["production"]
    assert managed["environment"] == "production-host"
    contained = entries["scripts/check_contained_resize_smoke.py"]
    assert contained["profiles"] == ["ci", "production"]
    assert contained["environment"] == "xvfb-dbus"
    assert contained["timeout_seconds"] == 300
    web = entries["scripts/check_web_readonly_smoke.py"]
    assert web["profiles"] == ["ci", "production"]
    assert web["environment"] == "xvfb-dbus"
    assert web["timeout_seconds"] == 600
    for path, entry in entries.items():
        if path == managed["path"]:
            continue
        assert entry["profiles"] == ["ci", "production"]
        assert entry["environment"] == "xvfb-dbus"


def test_profile_selection_is_closed_and_complete() -> None:
    manifest, _raw, tests, _smokes = runner.load_manifest(REPO_ROOT, MANIFEST)
    ci_tests, ci_smokes = runner.selected_inventory(
        manifest,
        profile="ci",
        tests_only=False,
        smokes_only=False,
        discovered_tests=tests,
    )
    production_tests, production_smokes = runner.selected_inventory(
        manifest,
        profile="production",
        tests_only=False,
        smokes_only=False,
        discovered_tests=tests,
    )
    assert ci_tests == production_tests == tests
    assert len(ci_smokes) == 24
    assert len(production_smokes) == 25
    assert all(entry["environment"] == "xvfb-dbus" for entry in ci_smokes)
    assert sum(entry["environment"] == "production-host" for entry in production_smokes) == 1


def test_manifest_rejects_inventory_drift_duplicates_and_unsafe_paths(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    payload = _fixture_manifest()
    runner.validate_manifest_payload(
        root,
        payload,
        discovered_tests=["tests/test_dummy.py"],
        discovered_smokes=["scripts/check_demo_smoke.py"],
    )

    missing = copy.deepcopy(payload)
    missing["smokes"] = []
    with pytest.raises(runner.ValidationError, match="nonempty"):
        runner.validate_manifest_payload(
            root,
            missing,
            discovered_tests=["tests/test_dummy.py"],
            discovered_smokes=["scripts/check_demo_smoke.py"],
        )

    duplicate = copy.deepcopy(payload)
    duplicate["smokes"] = [duplicate["smokes"][0], copy.deepcopy(duplicate["smokes"][0])]
    with pytest.raises(runner.ValidationError, match="sorted and unique"):
        runner.validate_manifest_payload(
            root,
            duplicate,
            discovered_tests=["tests/test_dummy.py"],
            discovered_smokes=["scripts/check_demo_smoke.py"],
        )

    traversal = copy.deepcopy(payload)
    traversal["smokes"][0]["path"] = "../check_escape_smoke.py"
    with pytest.raises(runner.ValidationError, match="safe direct relative"):
        runner.validate_manifest_payload(
            root,
            traversal,
            discovered_tests=["tests/test_dummy.py"],
            discovered_smokes=["scripts/check_demo_smoke.py"],
        )

    symlink = root / "scripts" / "check_link_smoke.py"
    symlink.symlink_to("check_demo_smoke.py")
    linked = copy.deepcopy(payload)
    linked["smokes"][0]["path"] = "scripts/check_link_smoke.py"
    with pytest.raises(runner.ValidationError, match="direct regular"):
        runner.validate_manifest_payload(
            root,
            linked,
            discovered_tests=["tests/test_dummy.py"],
            discovered_smokes=["scripts/check_link_smoke.py"],
        )


def test_strict_json_and_smoke_contract_reject_ambiguous_output() -> None:
    with pytest.raises(runner.ValidationError, match="duplicate JSON key"):
        runner.strict_json_loads('{"ok":true,"ok":false}')
    with pytest.raises(runner.ValidationError, match="invalid JSON"):
        runner.parse_smoke_output(b"not-json\n", limit=1024)
    with pytest.raises(runner.ValidationError, match="ok=true"):
        runner.parse_smoke_output(b'{"ok":false}\n', limit=1024)
    with pytest.raises(runner.ValidationError, match="nonempty reason"):
        runner.parse_smoke_output(b'{"ok":true,"skipped":true}\n', limit=1024)
    assert runner.parse_smoke_output(
        b'{"ok":true,"skipped":true,"reason":"dependency unavailable"}\n',
        limit=1024,
    ) == {"ok": True, "skipped": True, "reason": "dependency unavailable"}


def test_process_runner_bounds_output_and_kills_timeouts_without_limiting_data_files(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    environment = os.environ.copy()

    success = runner.run_process(
        task_id="success",
        kind="test",
        relative="success",
        argv=[sys.executable, "-c", "print('1 passed')"],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    assert success["ok"] is True
    assert runner.parse_test_summary((output / success["stdout"]["path"]).read_text())["passed"] == 1

    timeout = runner.run_process(
        task_id="timeout",
        kind="test",
        relative="timeout",
        argv=[sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=1,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    assert timeout["ok"] is False
    assert timeout["timed_out"] is True

    overflow = runner.run_process(
        task_id="overflow",
        kind="test",
        relative="overflow",
        argv=[sys.executable, "-c", "import sys;sys.stdout.write('x'*1000000);sys.stdout.flush()"],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    assert overflow["ok"] is False
    assert overflow["output_limit_exceeded"] is True
    assert overflow["stdout"]["size"] <= 4096
    assert overflow["stdout"]["observed_size"] > 4096

    data_path = tmp_path / "legitimate-large-data.bin"
    data_writer = runner.run_process(
        task_id="data-writer",
        kind="test",
        relative="data-writer",
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(data_path)!r}).write_bytes(b'x'*100000);print('1 passed')",
        ],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    assert data_writer["ok"] is True
    assert data_path.stat().st_size == 100000


def test_process_runner_drains_short_output_before_receipt_when_descendant_holds_pipe(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    descendant_pid_path = tmp_path / "descendant.pid"
    command = (
        "import pathlib,subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"pathlib.Path({str(descendant_pid_path)!r}).write_text(str(child.pid));"
        "print('1 passed',flush=True)"
    )

    started = time.monotonic()
    task = runner.run_process(
        task_id="inherited-output-pipe",
        kind="test",
        relative="inherited-output-pipe",
        argv=[sys.executable, "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        output_root=output,
        timeout_seconds=10,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    elapsed = time.monotonic() - started
    stdout_path = output / task["stdout"]["path"]

    assert task["ok"] is True, task
    assert task["output_drain"] == {
        "alive_readers": [],
        "complete": True,
        "errors": [],
        "group_cleanup_required": True,
    }
    assert task["stdout"]["observed_size"] == len(b"1 passed\n")
    assert task["stdout"]["size"] == len(b"1 passed\n")
    assert not any(
        thread.name.startswith("validation-inherited-output-pipe-")
        for thread in runner.threading.enumerate()
    )
    assert stdout_path.read_bytes() == b"1 passed\n"
    assert runner.parse_test_summary(stdout_path.read_text())["passed"] == 1
    assert elapsed < 6.0

    descendant_pid = int(descendant_pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)
    captured = stdout_path.read_bytes()
    time.sleep(0.25)
    assert stdout_path.read_bytes() == captured


def test_ci_xvfb_environment_uses_portable_private_runtime_and_cleans_after_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / ("deep-validation-work-" * 8)
    work.mkdir(parents=True)
    portable_base = tmp_path / "portable-tmp"
    portable_base.mkdir(mode=0o1777)
    portable_base.chmod(0o1777)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", portable_base)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/host/bus")

    environment, runtime = runner._task_environment(
        REPO_ROOT,
        work,
        environment="xvfb-dbus",
        profile="ci",
    )

    assert runtime is not None
    assert runtime.path.parent == runner._safe_task_runtime_base(
        runner._configured_task_runtime_base()
    )
    assert len(os.fsencode(runtime.path)) <= runner._TASK_RUNTIME_MAX_PATH_BYTES
    assert stat.S_IMODE(runtime.path.stat().st_mode) == 0o700
    assert environment["XDG_RUNTIME_DIR"] == str(runtime.path)
    assert environment["TMUX_TMPDIR"] == str(runtime.path)
    assert environment[runner._TASK_RUNTIME_BASE_ENV] == str(runtime.path)
    assert environment[runner._TASK_RUNTIME_POLICY_ENV] == "ci"
    assert "DBUS_SESSION_BUS_ADDRESS" not in environment
    for key in ("TMPDIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
        path = Path(environment[key])
        assert runtime.path in path.parents
        assert stat.S_IMODE(path.stat().st_mode) == 0o700

    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    command = (
        "import os;from pathlib import Path;"
        "assert Path(os.environ['XDG_RUNTIME_DIR']).is_dir();"
        "assert Path(os.environ['TMPDIR']).is_dir();"
        "Path(os.environ['TMPDIR'],'witness').write_text('ok');"
        "print('1 passed')"
    )
    task = runner.run_process(
        task_id="short-runtime",
        kind="test",
        relative="short-runtime",
        argv=[sys.executable, "-c", command],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=15,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
        cleanup_runtime=runtime,
    )

    assert task["ok"] is True, task
    assert task["runtime"]["external"] is True
    assert task["runtime"]["cleanup_ok"] is True
    assert not runtime.path.exists()


def test_validation_child_environment_excludes_ambient_secrets_and_shell_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "OPENAI_API_KEY": "synthetic-openai-secret",
        "GITHUB_TOKEN": "synthetic-github-secret",
        "AWS_SECRET_ACCESS_KEY": "synthetic-aws-secret",
        "TERMDECK_AUTH_COOKIE": "synthetic-auth-cookie",
        "SSH_AUTH_SOCK": "/synthetic/private-agent.sock",
        "BASH_ENV": "/synthetic/bash-env",
        "ENV": "/synthetic/sh-env",
        "LD_PRELOAD": "/synthetic/preload.so",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("T1000_EXPECTED_SITE", "/synthetic/expected-site")

    runtime_base = tmp_path / "runtime-base"
    runtime_base.mkdir(mode=0o700)
    runtime_base.chmod(0o700)
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(runtime_base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    work = tmp_path / "work"
    work.mkdir()
    environment, runtime = runner._task_environment(
        REPO_ROOT,
        work,
        environment="isolated",
    )

    assert runtime is not None
    assert environment["T1000_EXPECTED_SITE"] == "/synthetic/expected-site"
    assert not set(secrets).intersection(environment)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    task = runner.run_process(
        task_id="sanitized-environment",
        kind="test",
        relative="sanitized-environment",
        argv=[
            sys.executable,
            "-P",
            "-B",
            "-c",
            "import json,os;print(json.dumps(dict(os.environ),sort_keys=True))",
        ],
        cwd=work,
        env={**environment, **secrets},
        output_root=output,
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=16 * 1024,
        stderr_limit=4096,
        cleanup_runtime=runtime,
    )

    assert task["ok"] is True, task
    assert task["runtime"]["cleanup_ok"] is True
    stdout = (output / task["stdout"]["path"]).read_text(encoding="utf-8")
    stderr = (output / task["stderr"]["path"]).read_text(encoding="utf-8")
    child_environment = json.loads(stdout)
    assert not set(secrets).intersection(child_environment)
    encoded_evidence = stdout + stderr + json.dumps(task, sort_keys=True)
    for key, value in secrets.items():
        assert key not in encoded_evidence
        assert value not in encoded_evidence


def test_inventory_and_tool_probe_children_use_the_closed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-tool-probe-secret")
    monkeypatch.setenv("BASH_ENV", "/synthetic/tool-probe-shell-hook")
    observed: list[dict[str, str]] = []

    def completed(argv, **kwargs):
        observed.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(runner.subprocess, "run", completed)

    assert runner._git_source_paths(tmp_path) == []
    result = runner._tool_output([sys.executable, "--version"], cwd=tmp_path)

    assert result["available"] is True
    assert len(observed) == 2
    for environment in observed:
        assert "GITHUB_TOKEN" not in environment
        assert "BASH_ENV" not in environment
        assert environment["PATH"]


def test_normal_validation_tmux_argv_emits_no_debug_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = runner.shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is unavailable")
    with runner.tempfile.TemporaryDirectory(prefix="tv-", dir="/tmp") as raw_base:
        runtime_base = Path(raw_base)
        runtime_base.chmod(0o700)
        monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(runtime_base))
        work = tmp_path / ("deep-validation-shard-" * 8)
        work.mkdir()
        assert len(os.fsencode(work / "tmp")) >= 121

        environment, runtime = runner._task_environment(
            REPO_ROOT,
            work,
            environment="isolated",
        )
        assert runtime is not None
        assert environment["TMUX_TMPDIR"] == str(runtime.path)
        assert len(os.fsencode(runtime.path)) <= runner._TASK_RUNTIME_MAX_PATH_BYTES
        socket = "validation-no-debug"
        command = (
            "import subprocess;"
            f"tmux={tmux!r};socket={socket!r};"
            "subprocess.run([tmux,'-L',socket,'new-session','-d','/bin/sh','-c','exit 0'],check=True);"
            "subprocess.run([tmux,'-L',socket,'kill-server'],check=False);"
            "print('1 passed')"
        )
        output = tmp_path / "output"
        output.mkdir(mode=0o700)
        task = runner.run_process(
            task_id="tmux-no-debug",
            kind="test",
            relative="tmux-no-debug",
            argv=[sys.executable, "-P", "-B", "-c", command],
            cwd=work,
            env=environment,
            output_root=output,
            timeout_seconds=10,
            grace_seconds=1,
            stdout_limit=4096,
            stderr_limit=4096,
            cleanup_runtime=runtime,
        )

        assert task["ok"] is True, task
        assert task["runtime"]["cleanup_ok"] is True
        assert not runtime.path.exists()
        assert not list(work.glob("tmux-client-*.log"))
        assert not list(work.glob("tmux-server-*.log"))
        assert not list(work.glob("tmux-out-*.log"))


def test_xvfb_smoke_argv_uses_glx_safe_server_with_private_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/host/bus")
    executables = {
        "xvfb-run": "/usr/bin/xvfb-run",
        "dbus-run-session": "/usr/bin/dbus-run-session",
    }
    monkeypatch.setattr(runner.shutil, "which", executables.__getitem__)

    environment, runtime = runner._task_environment(
        tmp_path,
        work,
        environment="xvfb-dbus",
        profile="ci",
    )
    assert runtime is not None
    try:
        assert runner._build_smoke_argv(
            {"environment": "xvfb-dbus", "path": "scripts/check_demo_smoke.py"},
            python="/usr/bin/python3.14",
            root=tmp_path,
        ) == [
            "/usr/bin/xvfb-run",
            "-a",
            "-s",
            "-screen 0 1280x1024x24 -extension GLX",
            "/usr/bin/dbus-run-session",
            "--",
            "/usr/bin/python3.14",
            "-P",
            "-B",
            str(tmp_path / "scripts/check_demo_smoke.py"),
        ]
        assert environment["XDG_RUNTIME_DIR"] == str(runtime.path)
        assert environment["TMUX_TMPDIR"] == str(runtime.path)
        assert environment[runner._TASK_RUNTIME_BASE_ENV] == str(runtime.path)
        assert environment[runner._TASK_RUNTIME_POLICY_ENV] == "ci"
        assert environment["TMPDIR"] == str(runtime.path / "t")
        assert environment["XDG_CACHE_HOME"] == str(runtime.path / "c")
        assert environment["PYTHONPYCACHEPREFIX"] == str(runtime.path / "p")
        assert "DBUS_SESSION_BUS_ADDRESS" not in environment
    finally:
        assert runner._cleanup_task_runtime(runtime)["cleanup_ok"] is True


def test_short_runtime_uses_validated_absolute_configured_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    runtime = runner._create_short_task_runtime(work)

    assert runtime.path.parent == base.resolve(strict=True)
    assert stat.S_IMODE(runtime.path.stat().st_mode) == 0o700
    assert runner._cleanup_task_runtime(runtime)["cleanup_ok"] is True


def test_short_runtime_rejects_relative_configured_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, "relative/runtime")

    with pytest.raises(
        runner.ValidationError,
        match="T1000_VALIDATION_RUNTIME_BASE must be an absolute path",
    ):
        runner._create_short_task_runtime(Path.cwd())


def test_portable_runtime_prefers_trusted_xdg_runtime_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_runtime = tmp_path / "xdg-runtime"
    xdg_runtime.mkdir(mode=0o700)
    xdg_runtime.chmod(0o700)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_runtime))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    base = runner._configured_task_runtime_base()

    assert base == xdg_runtime / runner._TASK_RUNTIME_PORTABLE_NAME
    assert base.parent == xdg_runtime
    assert base.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


def test_portable_runtime_fallback_is_stable_private_per_user_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_parent = tmp_path / "tmp"
    fallback_parent.mkdir(mode=0o1777)
    fallback_parent.chmod(0o1777)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", fallback_parent)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    first = runner._configured_task_runtime_base()
    second = runner._configured_task_runtime_base()

    assert first == second == fallback_parent / f"t1000-validation-{os.geteuid()}"
    assert first.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(first.stat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_kind", ("symlink", "mode"))
def test_portable_runtime_rejects_unsafe_existing_fallback_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    fallback_parent = tmp_path / "tmp"
    fallback_parent.mkdir(mode=0o1777)
    fallback_parent.chmod(0o1777)
    candidate = fallback_parent / f"t1000-validation-{os.geteuid()}"
    if unsafe_kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        candidate.symlink_to(target, target_is_directory=True)
    else:
        candidate.mkdir(mode=0o755)
        candidate.chmod(0o755)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", fallback_parent)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    with pytest.raises(
        runner.ValidationError,
        match="portable validation runtime base is not a private 0700 directory",
    ):
        runner._configured_task_runtime_base()


def test_portable_runtime_rejects_foreign_owned_fallback_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_parent = tmp_path / "tmp"
    fallback_parent.mkdir(mode=0o1777)
    fallback_parent.chmod(0o1777)
    foreign_uid = os.geteuid() + 1
    candidate = fallback_parent / f"t1000-validation-{foreign_uid}"
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o700)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", fallback_parent)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(runner.os, "geteuid", lambda: foreign_uid)

    with pytest.raises(
        runner.ValidationError,
        match="portable validation runtime base is not a private 0700 directory",
    ):
        runner._configured_task_runtime_base()


def test_production_xvfb_environment_fails_closed_without_private_ssd_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)

    with pytest.raises(
        runner.ValidationError,
        match="T1000_VALIDATION_RUNTIME_BASE is required for production smokes",
    ):
        runner._task_environment(
            REPO_ROOT,
            work,
            environment="xvfb-dbus",
            profile="production",
        )

    base = tmp_path / "production-runtime-base"
    base.mkdir(mode=0o1777)
    base.chmod(0o1777)
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    with pytest.raises(
        runner.ValidationError,
        match="is not a private 0700 directory",
    ):
        runner._task_environment(
            REPO_ROOT,
            work,
            environment="xvfb-dbus",
            profile="production",
        )

    base.chmod(0o700)
    import termdeck.storage_fabric as storage_fabric

    def reject_storage(**_kwargs: object) -> None:
        raise RuntimeError("rotational backing device")

    monkeypatch.setattr(storage_fabric, "require_hot_storage", reject_storage)

    with pytest.raises(
        runner.ValidationError,
        match="not verified Warp Drives SSD storage",
    ):
        runner._task_environment(
            REPO_ROOT,
            work,
            environment="xvfb-dbus",
            profile="production",
        )
    assert not list(base.iterdir())


def test_production_runtime_attestation_overrides_unrelated_default_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "production-runtime-base"
    base.mkdir(mode=0o700)
    base.chmod(0o700)
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    import termdeck.storage_fabric as storage_fabric

    observed: dict[str, object] = {}

    def accept_storage(**kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(storage_fabric, "require_hot_storage", accept_storage)

    assert runner._production_task_runtime_base() == base
    assert observed == {
        "hot_paths": {
            "auth_root": base,
            "checkpoint_root": base,
            "database_root": base,
            "runtime_root": base,
            "tmux_runtime": base,
        },
        "managed_root": base,
        "production": True,
    }


def test_production_host_environment_preserves_runtime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/authoritative/home")
    monkeypatch.setenv("TMPDIR", "/authoritative/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/authoritative/runtime")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/authoritative/bus")
    monkeypatch.setenv("T1000_MANAGED_ROOT", "/authoritative/managed-root")
    monkeypatch.setenv("T1000_MANAGED_BIN_DIR", "/authoritative/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-production-inventory")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/authoritative/private-agent.sock")
    monkeypatch.setenv("TMUX", "/authoritative/tmux,1,0")

    environment, runtime = runner._task_environment(
        REPO_ROOT,
        tmp_path,
        environment="production-host",
    )

    assert runtime is None
    assert environment["HOME"] == "/authoritative/home"
    assert environment["TMPDIR"] == "/authoritative/tmp"
    assert environment["XDG_RUNTIME_DIR"] == "/authoritative/runtime"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/authoritative/bus"
    assert environment["T1000_MANAGED_ROOT"] == "/authoritative/managed-root"
    assert environment["T1000_MANAGED_BIN_DIR"] == "/authoritative/bin"
    assert "OPENAI_API_KEY" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "TMUX" not in environment


def test_changed_short_runtime_identity_fails_task_and_is_not_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    base.chmod(0o700)
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    environment, runtime = runner._task_environment(
        REPO_ROOT,
        work,
        environment="xvfb-dbus",
        profile="ci",
    )
    assert runtime is not None
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    command = (
        "import os;"
        "os.chmod(os.environ['XDG_RUNTIME_DIR'],0o755);"
        "print('1 passed')"
    )

    task = runner.run_process(
        task_id="changed-runtime",
        kind="test",
        relative="changed-runtime",
        argv=[sys.executable, "-c", command],
        cwd=tmp_path,
        env=environment,
        output_root=output,
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
        cleanup_runtime=runtime,
    )

    assert task["returncode"] == 0
    assert task["ok"] is False
    assert task["runtime"]["cleanup_ok"] is False
    assert "identity, ownership, type, or mode changed" in task["runtime"]["error"]
    assert runtime.path.exists()
    runtime.path.chmod(0o700)
    assert runner._cleanup_task_runtime(runtime)["cleanup_ok"] is True


def test_short_runtime_has_owner_lease_and_bounded_dead_owner_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later validator may reclaim only one lease-bound dead owner."""

    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)

    runtime = runner._create_short_task_runtime(work)
    assert runtime.lease_path.is_file()
    assert stat.S_IMODE(runtime.lease_path.stat().st_mode) == 0o600
    lease = runner.strict_json_loads(runtime.lease_path.read_bytes())
    assert lease["pid"] == os.getpid()
    assert lease["device"] == runtime.device
    assert lease["inode"] == runtime.inode

    live = runner._reap_stale_task_runtimes(base=base)
    assert live["removed"] == 0
    assert live["retained_live"] == 1
    assert runtime.path.is_dir()

    monkeypatch.setattr(runner, "_runtime_owner_alive", lambda _lease: False)
    stale = runner._reap_stale_task_runtimes(base=base)
    assert stale["removed"] == 1
    assert stale["failed"] == 0
    assert not runtime.path.exists()


def test_short_runtime_reaper_bounds_raw_scans_and_advances_past_unrelated_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    for index in range(runner._TASK_RUNTIME_MAX_SCAN_ENTRIES + 32):
        (base / f"unrelated-{index:02d}").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    runtime = runner._create_short_task_runtime(work, base=base)
    monkeypatch.setattr(runner, "_runtime_owner_alive", lambda _lease: False)

    receipts = []
    for _attempt in range(80):
        observed = runner._reap_stale_task_runtimes(
            base=base,
            max_entries=5,
            max_removals=1,
        )
        receipts.append(observed)
        assert observed["scanned_entries"] <= 5
        if not runtime.path.exists():
            break

    assert sum(int(receipt["removed"]) for receipt in receipts) == 1
    assert not runtime.path.exists()


def test_short_runtime_reaper_fairly_removes_owned_malformed_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    malformed = []
    for index in range(9):
        candidate = base / f"t1v-deadbeef-{index:08x}"
        candidate.mkdir(mode=0o700)
        candidate.chmod(0o700)
        malformed.append(candidate)
    symlink = base / "t1v-feedface-deadbeef"
    symlink.symlink_to(tmp_path, target_is_directory=True)
    malformed.append(symlink)
    quarantine = base / f"t1v-q-{'a' * 32}"
    quarantine.write_text("malformed quarantine residue\n", encoding="utf-8")
    malformed.append(quarantine)
    work = tmp_path / "work"
    work.mkdir()
    runtime = runner._create_short_task_runtime(work, base=base)
    monkeypatch.setattr(runner, "_runtime_owner_alive", lambda _lease: False)

    receipts = []
    for _attempt in range(20):
        observed = runner._reap_stale_task_runtimes(
            base=base,
            max_entries=3,
            max_removals=2,
        )
        receipts.append(observed)
        assert observed["scanned_entries"] <= 3
        assert observed["removed"] <= 2
        if not runtime.path.exists() and not any(path.exists() or path.is_symlink() for path in malformed):
            break

    assert len(receipts) > 1
    assert any(receipt["cursor_continued"] is True for receipt in receipts[1:])
    assert sum(int(receipt["removed_malformed"]) for receipt in receipts) == len(
        malformed
    )
    assert not runtime.path.exists()
    assert not any(
        path.exists() or path.is_symlink()
        for path in malformed
    )


def test_short_runtime_reaper_bounds_candidates_and_removals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    runtimes = []
    for index in range(6):
        work = tmp_path / f"work-{index}"
        work.mkdir()
        runtimes.append(runner._create_short_task_runtime(work, base=base))
    monkeypatch.setattr(runner, "_runtime_owner_alive", lambda _lease: False)

    observed = runner._reap_stale_task_runtimes(
        base=base,
        max_entries=5,
        max_removals=2,
    )

    assert observed["inspected_entries"] == 5
    assert observed["removed"] == 2
    assert observed["entry_bound_reached"] is True
    assert observed["removal_bound_reached"] is True
    assert sum(runtime.path.exists() for runtime in runtimes) == 4


def test_source_fingerprint_is_deterministic_and_excludes_output(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "module.py"], cwd=root, check=True)
    output = root / "validation-output"
    output.mkdir()
    before = runner.source_fingerprint(root, excluded_root=output)
    (output / "receipt.json").write_text("generated\n", encoding="utf-8")
    after_output = runner.source_fingerprint(root, excluded_root=output)
    assert before == after_output
    (root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert runner.source_fingerprint(root, excluded_root=output) != before


def test_atomic_receipt_is_canonical_private_and_replaceable(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    runner.atomic_write_json(receipt, {"z": 1, "a": True})
    assert receipt.read_bytes() == b'{"a":true,"z":1}\n'
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    runner.atomic_write_json(receipt, {"ok": False})
    assert json.loads(receipt.read_text()) == {"ok": False}


def test_manifest_check_does_not_create_or_reap_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_repo(tmp_path, smoke_source="print('{\"ok\":true}')\n")
    output = tmp_path / "result"
    fallback_parent = tmp_path / "tmp"
    fallback_parent.mkdir(mode=0o1777)
    fallback_parent.chmod(0o1777)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", fallback_parent)

    def unexpected_reap(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("manifest checks must not reap runtime state")

    monkeypatch.setattr(runner, "_reap_stale_task_runtimes", unexpected_reap)
    args = argparse.Namespace(
        root=str(root),
        manifest="scripts/validation_manifest.json",
        profile="ci",
        output=str(output),
        python=sys.executable,
        tests_only=False,
        smokes_only=False,
        check_manifest=True,
    )

    code, receipt = runner.run_validation(args)

    assert code == 0
    assert receipt["ok"] is True
    assert "runtime_preflight" not in receipt
    assert not (fallback_parent / f"t1000-validation-{os.geteuid()}").exists()


def test_ci_smokes_only_runs_without_configured_runtime_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke_source = """import json, os, stat
from pathlib import Path
runtime = Path(os.environ['T1000_VALIDATION_RUNTIME_BASE'])
temporary = Path(os.environ['TMPDIR'])
assert os.environ['T1000_VALIDATION_RUNTIME_POLICY'] == 'ci'
assert runtime == Path(os.environ['TMUX_TMPDIR'])
assert runtime in temporary.parents
assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
print(json.dumps({'ok': True}))
"""
    root = _fixture_repo(tmp_path, smoke_source=smoke_source)
    output = tmp_path / "result"
    portable_base = tmp_path / "portable-tmp"
    portable_base.mkdir(mode=0o1777)
    portable_base.chmod(0o1777)
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", portable_base)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(
        runner,
        "_build_smoke_argv",
        lambda entry, *, python, root: [python, "-P", "-B", str(root / entry["path"])],
    )
    monkeypatch.setattr(runner, "tool_versions", lambda root, python: {})
    args = argparse.Namespace(
        root=str(root),
        manifest="scripts/validation_manifest.json",
        profile="ci",
        output=str(output),
        python=sys.executable,
        tests_only=False,
        smokes_only=True,
        check_manifest=False,
    )

    code, receipt = runner.run_validation(args)

    assert code == 0
    assert receipt["ok"] is True
    assert receipt["completed"] is True
    task = receipt["tasks"][0]
    assert task["summary"] == {"ok": True, "skipped": False}
    runtime_path = Path(task["runtime"]["path"])
    assert runtime_path.parent == (
        portable_base / f"t1000-validation-{os.geteuid()}"
    ).resolve(strict=True)
    assert task["runtime"]["cleanup_ok"] is True
    assert not runtime_path.exists()


def test_production_smokes_only_fails_before_task_without_runtime_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_repo(tmp_path, smoke_source="print('{\"ok\":true}')\n")
    output = tmp_path / "result"
    monkeypatch.delenv(runner._TASK_RUNTIME_BASE_ENV, raising=False)
    monkeypatch.setattr(runner, "tool_versions", lambda root, python: {})
    args = argparse.Namespace(
        root=str(root),
        manifest="scripts/validation_manifest.json",
        profile="production",
        output=str(output),
        python=sys.executable,
        tests_only=False,
        smokes_only=True,
        check_manifest=False,
    )

    code, receipt = runner.run_validation(args)

    assert code == 1
    assert receipt["ok"] is False
    assert receipt["completed"] is False
    assert receipt["tasks"] == []
    assert "T1000_VALIDATION_RUNTIME_BASE is required for production smokes" in receipt[
        "failures"
    ][0]["reason"]


def test_runner_writes_durable_failure_receipt_for_malformed_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_repo(tmp_path)
    output = tmp_path / "result"
    runtime_base = tmp_path / "short-runtime-base"
    runtime_base.mkdir(mode=0o700)
    runtime_base.chmod(0o700)
    monkeypatch.setenv(runner._TASK_RUNTIME_BASE_ENV, str(runtime_base))
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(
        runner,
        "_build_smoke_argv",
        lambda entry, *, python, root: [python, "-P", "-B", str(root / entry["path"])],
    )
    monkeypatch.setattr(runner, "tool_versions", lambda root, python: {})
    args = argparse.Namespace(
        root=str(root),
        manifest="scripts/validation_manifest.json",
        profile="ci",
        output=str(output),
        python=sys.executable,
        tests_only=False,
        smokes_only=True,
        check_manifest=False,
    )
    code, receipt = runner.run_validation(args)
    assert code == 1
    assert receipt["ok"] is False
    assert receipt["completed"] is True
    assert receipt["failures"]
    persisted = json.loads((output / "receipt.json").read_text())
    assert persisted["ok"] is False
    assert persisted["tasks"][0]["stdout"]["sha256"]
    assert stat.S_IMODE((output / "receipt.json").stat().st_mode) == 0o600
