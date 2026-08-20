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
    assert len(smokes) == 21
    entries = {entry["path"]: entry for entry in payload["smokes"]}
    assert sorted(entries) == smokes
    managed = entries["scripts/check_managed_process_inventory_smoke.py"]
    assert managed["profiles"] == ["production"]
    assert managed["environment"] == "production-host"
    contained = entries["scripts/check_contained_resize_smoke.py"]
    assert contained["profiles"] == ["ci", "production"]
    assert contained["environment"] == "xvfb-dbus"
    assert contained["timeout_seconds"] == 300
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
    assert len(ci_smokes) == 20
    assert len(production_smokes) == 21
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


def test_xvfb_environment_uses_short_private_runtime_and_cleans_after_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / ("deep-validation-work-" * 8)
    work.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/host/bus")

    environment, runtime = runner._task_environment(
        REPO_ROOT,
        work,
        environment="xvfb-dbus",
    )

    assert runtime is not None
    assert runtime.path.parent == runner._TASK_RUNTIME_BASE.resolve(strict=True)
    assert len(os.fsencode(runtime.path)) <= runner._TASK_RUNTIME_MAX_PATH_BYTES
    assert stat.S_IMODE(runtime.path.stat().st_mode) == 0o700
    assert environment["XDG_RUNTIME_DIR"] == str(runtime.path)
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
        timeout_seconds=5,
        grace_seconds=1,
        stdout_limit=4096,
        stderr_limit=4096,
        cleanup_runtime=runtime,
    )

    assert task["ok"] is True
    assert task["runtime"]["external"] is True
    assert task["runtime"]["cleanup_ok"] is True
    assert not runtime.path.exists()


def test_production_host_environment_preserves_runtime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/authoritative/home")
    monkeypatch.setenv("TMPDIR", "/authoritative/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/authoritative/runtime")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/authoritative/bus")

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


def test_changed_short_runtime_identity_fails_task_and_is_not_removed(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    environment, runtime = runner._task_environment(
        REPO_ROOT,
        work,
        environment="xvfb-dbus",
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
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", base)
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


def test_short_runtime_reaper_consumes_only_total_entry_and_removal_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "short-runtime-base"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_BASE", base)
    monkeypatch.setattr(runner, "_TASK_RUNTIME_MAX_PATH_BYTES", 4096)
    for index in range(12):
        (base / f"unrelated-{index:02d}").mkdir()

    observed = runner._reap_stale_task_runtimes(
        base=base,
        max_entries=5,
        max_removals=2,
    )

    assert observed["inspected_entries"] <= 5
    assert observed["removed"] <= 2
    assert observed["entry_bound_reached"] is True


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


def test_runner_writes_durable_failure_receipt_for_malformed_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_repo(tmp_path)
    output = tmp_path / "result"
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
