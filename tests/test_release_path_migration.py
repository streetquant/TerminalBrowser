from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from termdeck import cli
from termdeck import release_manager as manager
from termdeck.build_info import MANAGED_STATE_SCHEMA, canonical_json_bytes, sha256_bytes, sha256_file
from test_release_manager import write_manifest, write_wheel


@pytest.fixture
def wheels(tmp_path: Path):
    """Real importable wheels; only their provenance IDs are synthetic fixtures."""
    package = Path(manager.__file__).parent
    members = {
        "termdeck/" + str(path.relative_to(package)): path.read_bytes()
        for path in package.rglob("*.py") if path.name != "__init__.py"
    }
    # The disposable launcher tests exercise authenticated path migration, not
    # host storage admission.  Keep the wheel importable while avoiding a
    # false failure on CloudMCP's overlay filesystem; production admission is
    # asserted independently below by test_migration_keeps_production_attestation_mandatory.
    members["termdeck/storage_fabric.py"] += (
        "\n\ndef require_hot_storage(**kwargs):\n"
        "    return None\n"
    ).encode()
    result = []
    for seed in ("a", "b"):
        wheel = tmp_path / seed / "t1000-0.2.21-py3-none-any.whl"
        info = write_wheel(wheel, seed=seed, version="0.2.21", extra_members=members)
        manifest_path = wheel.with_suffix(".release.json")
        manifest = write_manifest(manifest_path, wheel, info)
        result.append((wheel, manifest_path, manifest))
    return result


@pytest.fixture
def relocated(tmp_path: Path, wheels):
    """Construct the historical pre-wrapper on-disk schema, then copy it.

    This fixture does not remove history from a contemporary installation or
    modify a real managed root. Its activation bytes come from the same source
    packed into the selected immutable wheel.
    """
    old = manager.release_layout(root=tmp_path / "old" / "managed", bin_dir=tmp_path / "old" / "bin")
    wheel, manifest_path, manifest = wheels[0]
    verified = manager.verify_wheel(wheel, manifest_path=manifest_path)
    with manager.release_lock(old):
        manager._stage_release(old, verified, manifest_path)
        interpreter = Path(sys.executable).resolve(strict=True)
        activation = manager._current_activation_bundle(old, interpreter)
        state = manager.validate_managed_state({
            "schema": MANAGED_STATE_SCHEMA,
            "generation": 1,
            "current_release_id": manifest["release_id"],
            "previous_release_id": None,
            "manifest_sha256": sha256_file(old.releases_dir / manifest["release_id"] / "manifest.json"),
            "python_executable": str(interpreter),
            "bootstrap_sha256": activation.bootstrap_sha256,
            "launcher_path": str(old.launcher_path),
            "launcher_sha256": activation.launcher_sha256,
            "legacy_launcher": None,
        })
        manager._atomic_write_bytes(old.bootstrap_path, activation.bootstrap, mode=0o755)
        manager._atomic_write_bytes(old.launcher_path, activation.launcher, mode=0o755)
        manager._atomic_write_bytes(old.state_path, canonical_json_bytes(state), mode=0o600)
    new = manager.release_layout(root=tmp_path / "new" / "managed", bin_dir=tmp_path / "new" / "bin")
    shutil.copytree(old.root, new.root)
    shutil.copytree(old.bin_dir, new.bin_dir)
    user_state = tmp_path / "new" / "user-data.json"
    user_state.write_text('{"tabs":["preserve"],"cwd":"/work","auth":"fixture"}\n')
    return old, new, user_state


def migrate(relocated, wheels, **kwargs):
    old, new, _ = relocated
    return manager.install_release(
        wheels[1][0], wheels[1][1], root=new.root, bin_dir=new.bin_dir,
        migrate_from_root=old.root, migrate_from_bin_dir=old.bin_dir, **kwargs,
    )


def activation_bytes(layout):
    return {
        path: path.read_bytes() if path.exists() else None
        for path in (layout.state_path, layout.bootstrap_path, layout.launcher_path)
    }


def run_launcher(layout, *args):
    return subprocess.run([str(layout.launcher_path), *args], text=True, capture_output=True, timeout=30)


def test_disposable_wrapper_layout_keeps_canonical_identity_and_local_outputs(tmp_path: Path):
    fixture_root = tmp_path / "fixture"
    layout = manager.release_layout(
        root=fixture_root / "managed",
        bin_dir=fixture_root / "bin",
    )

    kwargs = manager._wrapper_layout_kwargs(layout)

    assert kwargs["source_root"] == manager._CANONICAL_SOURCE_ROOT
    assert ".bootstrap-import-" not in str(kwargs["source_root"])
    assert kwargs["runtime_root"] == fixture_root
    assert kwargs["state_root"] == fixture_root / "state"
    assert kwargs["managed_root"] == layout.root
    assert kwargs["managed_bin"] == layout.bin_dir
    assert kwargs["unit_dir"] == fixture_root / "units"
    assert kwargs["control_root"] == tmp_path / "fixture-termdeck-control"

    wrapper_paths = manager._wrapper_path_set(
        manager._service_wrapper_api(),
        kwargs,
        "a" * 64,
    )
    assert wrapper_paths
    assert all(path.is_relative_to(tmp_path) for path in wrapper_paths)
    assert all(not path.is_relative_to(manager._DEPLOYMENT_RUNTIME_ROOT) for path in wrapper_paths)


def test_plain_install_rejects_relocated_binding(relocated, wheels):
    _old, new, _ = relocated
    before = activation_bytes(new)
    with pytest.raises(manager.ReleaseError, match="stable launcher path"):
        manager.install_release(wheels[1][0], wheels[1][1], root=new.root, bin_dir=new.bin_dir)
    assert activation_bytes(new) == before


@pytest.mark.parametrize("launcher_kind", ["copied", "already-rendered", "missing"])
def test_migration_runs_real_launcher_and_release_rollback(relocated, wheels, launcher_kind):
    old, new, user_state = relocated
    old_before = activation_bytes(old)
    user_before = user_state.read_bytes()
    if launcher_kind == "already-rendered":
        manager._atomic_write_bytes(
            new.launcher_path, manager._launcher_source(new, Path(sys.executable).resolve()), mode=0o755,
        )
    elif launcher_kind == "missing":
        new.launcher_path.unlink()
    failed = subprocess.run(
        [sys.executable, "-I", "-B", str(new.bootstrap_path), "--help"],
        text=True, capture_output=True, timeout=30,
    )
    assert failed.returncode != 0
    assert "managed bootstrap path mismatch" in failed.stderr
    prior = activation_bytes(new)
    result = migrate(relocated, wheels)
    assert result["ok"] and result["live_processes_restarted"] is False
    state = manager.load_managed_state(new)
    assert state["launcher_path"] == str(new.launcher_path)
    assert state["current_release_id"] == wheels[1][2]["release_id"]
    assert state["previous_release_id"] == wheels[0][2]["release_id"]
    receipt = json.loads(Path(result["path_migration"]["receipt_path"]).read_bytes())
    assert receipt["prior_state_sha256"] == sha256_bytes(prior[new.state_path])
    assert receipt["published_state_sha256"] == sha256_bytes(new.state_path.read_bytes())
    assert receipt["historical_paths_written"] is False
    help_result = run_launcher(new, "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "usage:" in help_result.stdout.lower()
    info = run_launcher(new, "release", "info", "--root", str(new.root), "--bin-dir", str(new.bin_dir), "--verify")
    assert info.returncode == 0, info.stderr + info.stdout
    assert json.loads(info.stdout)["ok"] is True
    assert state["current_release_id"] in info.stdout
    # Normal reinstall is idempotent; callers omit the one-time migration flags.
    again = manager.install_release(wheels[1][0], wheels[1][1], root=new.root, bin_dir=new.bin_dir)
    assert again["idempotent"] is True
    rolled = manager.rollback_release(root=new.root, bin_dir=new.bin_dir)
    assert rolled["release_id"] == wheels[0][2]["release_id"]
    assert run_launcher(new, "--help").returncode == 0
    assert activation_bytes(old) == old_before
    assert user_state.read_bytes() == user_before


def test_migrated_launcher_kill_and_hide_use_only_disposable_terminal_state(relocated, wheels, tmp_path: Path):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is required for disposable launcher action verification")

    old, new, user_state = relocated
    old_before = activation_bytes(old)
    user_before = user_state.read_bytes()
    assert migrate(relocated, wheels)["ok"] is True

    # Keep the disposable tmux socket below AF_UNIX's path-length limit while
    # remaining inside this checkout's write boundary.  The unique server is
    # removed in the finally block and never shares the admitted live runtime.
    runtime = Path(
        tempfile.mkdtemp(
            prefix=".tmux-migration-test-",
            dir=manager._CANONICAL_SOURCE_ROOT,
        )
    )
    runtime.chmod(0o700)
    database = tmp_path / "sessions.sqlite3"
    environment = dict(os.environ)
    environment["TMUX_TMPDIR"] = str(runtime)
    environment["TERMDECK_TMUX_RUNTIME"] = str(runtime)
    environment.pop("TMUX", None)

    from termdeck.session_store import SessionStore

    store = SessionStore(database)
    try:
        hidden = store.create(
            "disposable-hide",
            "bootstrap-test",
            "sleep 300",
            str(tmp_path),
            window_id="disposable",
            window_title="Disposable",
            tab_index=0,
        )
        killed = store.create(
            "disposable-kill",
            "bootstrap-test",
            "sleep 300",
            str(tmp_path),
            window_id="disposable",
            window_title="Disposable",
            tab_index=1,
        )
    finally:
        store.close()

    def run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [tmux, *args],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if check:
            assert result.returncode == 0, result.stderr + result.stdout
        return result

    try:
        store = SessionStore(database)
        try:
            for record in (hidden, killed):
                run_tmux("new-session", "-d", "-s", record.tmux_name, "sleep", "300")
                assert run_tmux("has-session", "-t", record.tmux_name, check=False).returncode == 0
                store.update(record.id, status="running")
        finally:
            store.close()

        hide_result = subprocess.run(
            [str(new.launcher_path), "hide", hidden.id, "--db", str(database)],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert hide_result.returncode == 0, hide_result.stderr + hide_result.stdout
        hide_payload = json.loads(hide_result.stdout)
        assert hide_payload["ok"] is True
        assert hide_payload["sessions"][0]["id"] == hidden.id
        assert hide_payload["sessions"][0]["visible"] is False
        assert run_tmux("has-session", "-t", hidden.tmux_name, check=False).returncode == 0

        kill_result = subprocess.run(
            [str(new.launcher_path), "kill", killed.id, "--db", str(database), "--yes"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert kill_result.returncode == 0, kill_result.stderr + kill_result.stdout
        kill_payload = json.loads(kill_result.stdout)
        assert kill_payload["action"] == "kill"
        assert kill_payload["count"] == 1
        assert kill_payload["sessions"][0]["ok"] is True
        assert kill_payload["sessions"][0]["id"] == killed.id
        assert run_tmux("has-session", "-t", killed.tmux_name, check=False).returncode != 0
        assert run_tmux("has-session", "-t", hidden.tmux_name, check=False).returncode == 0

        store = SessionStore(database)
        try:
            hidden_after = store.get(hidden.id)
            killed_after = store.get(killed.id)
        finally:
            store.close()
        assert hidden_after is not None and hidden_after.visible is False
        assert hidden_after.status == "running"
        assert killed_after is not None and killed_after.visible is False
        assert killed_after.status == "stopped"
        assert killed_after.recovery_command is None
        assert killed_after.recovery_cwd is None
    finally:
        run_tmux("kill-server", check=False)
        shutil.rmtree(runtime, ignore_errors=True)

    assert activation_bytes(old) == old_before
    assert user_state.read_bytes() == user_before
    assert database.is_relative_to(tmp_path)
    assert runtime.parent == manager._CANONICAL_SOURCE_ROOT
    assert not runtime.is_relative_to(manager._DEPLOYMENT_RUNTIME_ROOT)
    assert not runtime.exists()


@pytest.mark.parametrize("target", ["bootstrap", "launcher", "manifest", "state-hash", "symlink"])
def test_migration_rejects_tamper_without_overwriting(relocated, wheels, target):
    old, new, user_state = relocated
    old_before = activation_bytes(old)
    if target == "bootstrap":
        new.bootstrap_path.write_bytes(new.bootstrap_path.read_bytes() + b"\n# changed\n")
    elif target == "launcher":
        new.launcher_path.write_bytes(b"#!/bin/sh\necho unrelated\n")
    elif target == "manifest":
        path = new.releases_dir / wheels[0][2]["release_id"] / "manifest.json"
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b" ")
    elif target == "state-hash":
        state = manager.load_managed_state(new)
        state["bootstrap_sha256"] = "0" * 64
        new.state_path.write_bytes(canonical_json_bytes(state))
    else:
        new.bootstrap_path.unlink()
        new.bootstrap_path.symlink_to(old.bootstrap_path)
    before = activation_bytes(new)
    with pytest.raises(manager.ReleaseError):
        migrate(relocated, wheels)
    assert activation_bytes(new) == before
    assert activation_bytes(old) == old_before
    assert not list(new.root.glob("path-migration-*.json"))
    assert "preserve" in user_state.read_text()


def test_migration_failed_publication_restores_preimages(relocated, wheels):
    old, new, user_state = relocated
    before = activation_bytes(new)
    old_before = activation_bytes(old)
    with mock.patch.object(manager, "_verify_managed_activation", side_effect=manager.ReleaseError("injected migration verification failure")):
        with pytest.raises(manager.ReleaseError, match="injected migration verification failure"):
            migrate(relocated, wheels)
    assert activation_bytes(new) == before
    assert activation_bytes(old) == old_before
    assert not list(new.root.glob("path-migration-*.json"))
    assert not (new.root / "wrapper-manifest.json").exists()
    assert "preserve" in user_state.read_text()
    assert migrate(relocated, wheels)["ok"] is True


def test_migration_keeps_production_attestation_mandatory(relocated, wheels):
    _old, new, _ = relocated
    before = activation_bytes(new)
    with mock.patch.object(manager, "_require_managed_storage", side_effect=manager.ReleaseError("attestation required")) as admission:
        with pytest.raises(manager.ReleaseError, match="attestation required"):
            migrate(relocated, wheels, production=True)
    admission.assert_called_once()
    assert activation_bytes(new) == before


def test_migration_refuses_existing_path_bound_history(relocated, wheels):
    _old, new, _ = relocated
    history = manager._legacy_wrapper_preimage_path(new)
    history.parent.mkdir(parents=True)
    history.write_bytes(b"preserve an unsupported historical receipt\n")
    before = activation_bytes(new)
    with pytest.raises(manager.ReleaseError, match="path-bound history"):
        migrate(relocated, wheels)
    assert history.read_bytes() == b"preserve an unsupported historical receipt\n"
    assert activation_bytes(new) == before


def test_migration_requires_both_explicit_historical_paths(relocated, wheels):
    old, new, _ = relocated
    with pytest.raises(manager.ReleaseError, match="both source root"):
        manager.install_release(wheels[1][0], wheels[1][1], root=new.root, bin_dir=new.bin_dir, migrate_from_root=old.root)
    with pytest.raises(manager.ReleaseError, match="source launcher"):
        manager.install_release(wheels[1][0], wheels[1][1], root=new.root, bin_dir=new.bin_dir, migrate_from_root=old.root, migrate_from_bin_dir=old.bin_dir / "wrong")


def test_cli_forwards_migration_without_disabling_production(capsys):
    args = argparse.Namespace(wheel=Path("candidate.whl"), manifest=Path("candidate.json"),
                              migrate_from_root=Path("/old/managed"), migrate_from_bin_dir=Path("/old/bin"))
    with mock.patch.object(cli, "install_release", return_value={"ok": True}) as install:
        assert cli.cmd_release_install(args) == 0
    assert install.call_args.kwargs["production"] is True
    assert install.call_args.kwargs["migrate_from_root"] == Path("/old/managed")
    assert install.call_args.kwargs["migrate_from_bin_dir"] == Path("/old/bin")
    assert json.loads(capsys.readouterr().out)["ok"] is True
