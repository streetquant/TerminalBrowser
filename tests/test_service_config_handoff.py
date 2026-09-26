"""Activation ownership across real config CAS and abrupt process exits."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import time

import pytest

from termdeck import legacy_activation as legacy
from termdeck import service_wrappers as wrappers
from termdeck import web_activation as activation
from test_legacy_activation import case, install, stage_bootstrap
from test_web_activation import _inject_post_install_failure


def start(case):
    layout = wrappers.resolve_layout(**activation._wrapper_kwargs(case.paths))
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-backend.service"]
    wrappers._record_start_identity(spec, layout, case.candidate["release_id"], [sys.executable, "-c", "pass"])
    return wrappers._read_unit_config_snapshot(layout.config_path(spec), spec=spec)


def owned(case):
    receipt = activation._read(case.tx / "receipt.json")
    activation._assert_package_rollback_owned(case.paths, receipt, receipt["preimages"],
        current_release_id=case.candidate["release_id"], old_release_id=case.original_release)
    return receipt


def inverse(case):
    receipt = owned(case)
    legacy.restore_package(case.paths, case.tx, receipt,
        lambda name: activation._checkpoint_publication(case.paths, case.tx, receipt, name))
    assert {path: path.read_bytes() for path in case.original} == case.original
    assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
    assert all(path.read_bytes() == content for path, content in case.sentinels.items())


def child_exit(function):
    pid = os.fork()
    if pid == 0:
        try:
            function()
            os._exit(87)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(88)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 87


def test_unreceipted_legitimate_cas_reproduces_original_inverse_failure(case, monkeypatch):
    install(case, stage_bootstrap(case))
    monkeypatch.setattr(wrappers, "_publish_activation_config_transition", lambda *_args: False)
    _value, snapshot = start(case)
    with pytest.raises(activation.WebActivationError, match="without a lifecycle receipt"):
        owned(case)
    assert snapshot.path.stat().st_ino == snapshot.preimage.identity.inode


@pytest.mark.parametrize("boundary", ["intent", "published", "committed", "recovery"])
def test_hard_exit_replays_lifecycle_journal_before_exact_inverse(case, boundary):
    install(case, stage_bootstrap(case))

    def crash_transition():
        original = wrappers._config_transition_save
        def save(path, row, previous):
            result = original(path, row, previous)
            if row["receipt"]["state"] == ("published" if boundary == "recovery" else boundary):
                os._exit(87)
            return result
        wrappers._config_transition_save = save
        start(case)
    child_exit(crash_transition)
    if boundary == "recovery":
        def crash_recovery():
            original = wrappers._config_transition_save
            def save(path, row, previous):
                result = original(path, row, previous)
                if row["receipt"]["state"] == "recovered":
                    os._exit(87)
                return result
            wrappers._config_transition_save = save
            owned(case)
        child_exit(crash_recovery)
    if boundary == "intent":
        # Recovery may keep the predecessor inode. A later authorized start
        # must be able to reuse its deterministic transition name.
        owned(case)
        start(case)
    inverse(case)


@pytest.mark.parametrize("when", ["before", "after"])
def test_same_bytes_foreign_config_inode_has_no_handoff_authority(case, when):
    install(case, stage_bootstrap(case))
    path = case.paths.database.parent / "termdeck-web-backend.service.conf"
    if when == "after":
        start(case)
    activation._atomic(path, path.read_bytes(), 0o600)
    foreign = activation._fingerprint(path)
    with pytest.raises(activation.WebActivationError, match="without a lifecycle receipt"):
        owned(case)
    assert activation._fingerprint(path) == foreign


def test_config_handoff_refuses_replaced_inner_journal(case):
    install(case, stage_bootstrap(case))
    def crash():
        original = wrappers._config_transition_save
        def save(path, row, previous):
            result = original(path, row, previous)
            if row["receipt"]["state"] == "published":
                os._exit(87)
            return result
        wrappers._config_transition_save = save
        start(case)
    child_exit(crash)
    journals = list(case.paths.database.parent.glob(".termdeck-publication-config-*.json"))
    assert len(journals) == 1
    activation._atomic(journals[0], journals[0].read_bytes(), 0o600)
    before = activation._fingerprint(journals[0])
    with pytest.raises(activation.WebActivationError, match="journal inode changed"):
        owned(case)
    assert activation._fingerprint(journals[0]) == before


def test_config_handoff_compacts_more_than_64_authorized_transitions(case, monkeypatch):
    install(case, stage_bootstrap(case))
    receipt = activation._read(case.tx / "receipt.json")
    activation._persist(case.paths, case.tx, receipt, "activated")
    monkeypatch.setattr(wrappers, "_CONFIG_TRANSITION_LIMIT", 2)
    for _ in range(35):
        value, snapshot = start(case)
        value.update(pid=None, start_ticks=None, exe=None, argv=[], command_sha256=None, state="stopped")
        wrappers._atomic_write_json(snapshot.path, value, mode=0o600, snapshot=snapshot)
    assert len(list(case.tx.glob("config-checkpoint-*.json"))) == 1
    assert list(case.tx.glob("config-transition-*.json")) == []
    inverse(case)


@pytest.mark.parametrize("boundary", ["checkpoint", "cleanup"])
def test_compaction_hard_exit_replays_cleanup_before_next_lifecycle(case, boundary):
    install(case, stage_bootstrap(case))
    def crash():
        if boundary == "checkpoint":
            original = wrappers._config_transition_save
            def save(path, row, previous):
                result = original(path, row, previous)
                if row.get("schema") == wrappers._CONFIG_CHECKPOINT_SCHEMA and row["retired"]:
                    os._exit(87)
                return result
            wrappers._config_transition_save = save
        else:
            original = activation._publish_anchored
            def publish(path, content, mode, **kwargs):
                result = original(path, content, mode, **kwargs)
                if kwargs.get("label") == "retired config transition receipt":
                    os._exit(87)
                return result
            activation._publish_anchored = publish
        start(case)
    child_exit(crash)
    assert len(list(case.tx.glob("config-checkpoint-*.json"))) == 1
    assert len(list(case.tx.glob("config-transition-*.json"))) == (1 if boundary == "checkpoint" else 0)
    start(case)
    assert list(case.tx.glob("config-transition-*.json")) == []
    inverse(case)


def test_checkpoint_cleanup_preserves_foreign_receipt_inode(case):
    install(case, stage_bootstrap(case))
    def crash():
        original = wrappers._config_transition_save
        def save(path, row, previous):
            result = original(path, row, previous)
            if row.get("schema") == wrappers._CONFIG_CHECKPOINT_SCHEMA and row["retired"]:
                os._exit(87)
            return result
        wrappers._config_transition_save = save
        start(case)
    child_exit(crash)
    entry, = case.tx.glob("config-transition-*.json")
    activation._atomic(entry, entry.read_bytes(), 0o600)
    foreign = activation._fingerprint(entry)
    with pytest.raises(activation.WebActivationError, match="retired config transition receipt inode changed"):
        owned(case)
    assert activation._fingerprint(entry) == foreign


@pytest.mark.parametrize("change", ["root", "head", "parents", "journal"])
def test_checkpoint_rejects_changed_authority(case, change):
    install(case, stage_bootstrap(case))
    _value, snapshot = start(case)
    entry, = case.tx.glob("config-checkpoint-*.json")
    value = activation._read(entry)
    if change in {"root", "head"}:
        value[change]["identity"]["inode"] += 1
    elif change == "parents":
        del value["receipt"]["parents"]
    else:
        value["receipt"]["journal_path"] += ".foreign"
    activation._write(entry, value)
    before = activation._fingerprint(snapshot.path)
    with pytest.raises(activation.WebActivationError, match="config (lifecycle checkpoint|transition)"):
        owned(case)
    assert activation._fingerprint(snapshot.path) == before


@pytest.mark.parametrize("change", ["latest", "rollback-phase"])
def test_lifecycle_revalidates_context_under_its_lock(case, monkeypatch, change):
    from contextlib import contextmanager
    install(case, stage_bootstrap(case))
    original = wrappers._config_transition_lock
    @contextmanager
    def changed_context(path, guards):
        with original(path, guards):
            if change == "latest":
                activation._atomic(case.paths.latest, case.paths.latest.read_bytes(), 0o600)
            else:
                receipt = activation._read(case.tx / "receipt.json")
                activation._persist(case.paths, case.tx, receipt, "rollback-start")
            yield
    monkeypatch.setattr(wrappers, "_config_transition_lock", changed_context)
    path = case.paths.database.parent / "termdeck-web-backend.service.conf"
    before = activation._fingerprint(path)
    with pytest.raises(wrappers.WrapperError, match="context changed while acquiring"):
        start(case)
    assert activation._fingerprint(path) == before


def test_complete_execute_stops_disposable_backend_and_restores_legacy(case, monkeypatch):
    """Run the real wrapper start/stop policy against a disposable process."""
    _inject_post_install_failure(monkeypatch)
    monkeypatch.setattr(activation, "GENERATED_RUNTIME", ())
    monkeypatch.setattr(activation, "_publish_units", lambda *_args, **_kwargs: None)
    (case.paths.runtime / "auth").mkdir()
    backend = case.paths.runtime / "bin/termdeck-web-server"
    backend.parent.mkdir()
    backend.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(60)\n")
    backend.chmod(0o700)
    children = []
    kwargs = activation._wrapper_kwargs(case.paths)
    def start_backend():
        # On A restart the legacy wrapper intentionally does not exist.
        if activation._managed_release_id(case.paths) == case.original_release:
            return
        bound = activation._release_wrapper_kwargs(case.paths, case.candidate["release_id"])
        code = ("import json,sys; from termdeck import service_wrappers as w; "
                "w._admit_backend_tmux_runtime=lambda layout: None; "
                "w.run_wrapper('termdeck-web-backend.service','start',**json.loads(sys.argv[1]))")
        process = subprocess.Popen([sys.executable, "-c", code, json.dumps({**bound, "release_id": case.candidate["release_id"]}, default=str)])
        children.append(process)
        pid = process.pid
        spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-backend.service"]
        layout = wrappers.resolve_layout(**kwargs)
        for _ in range(200):
            value = wrappers._read_unit_config(layout.config_path(spec), spec=spec)
            if value["pid"] == pid and wrappers.verify_process_identity(value):
                raise RuntimeError("disposable backend start failure after config publication")
            time.sleep(0.01)
        pytest.fail("disposable backend did not reach its recorded identity")
    monkeypatch.setattr(activation, "_start_web", start_backend)
    monkeypatch.setattr(activation, "_stop_web", lambda: wrappers.stop_unit("termdeck-web-backend.service", **activation._release_wrapper_kwargs(case.paths, case.candidate["release_id"])))
    try:
        with pytest.raises(RuntimeError, match="disposable backend start failure"):
            activation.execute(case.paths, case.tx)
        final = activation._read(case.tx / "receipt.json")
        assert final["status"] == "rolled-back", final.get("rollback_error")
        assert {path: path.read_bytes() for path in case.original} == case.original
        assert all(not path.exists() for path in activation._wrapper_publication_paths(case.paths))
        assert all(path.read_bytes() == content for path, content in case.sentinels.items())
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
