"""Resume A's service preparation after the runtime inverse has completed.

The root publisher and Ed25519 attestor use disposable trees. Systemd is a
fixture, while its reported PIDs refer to real, owned disposable processes;
the activation and wrapper code still check actual /proc identities.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from termdeck import runtime_attestation as attestation
from termdeck import service_wrappers as wrappers
from termdeck import web_activation as activation
from test_web_activation_consumer import consumer_generation, _inverse_fixture
import test_termdeck_preparer_publication as publication_fixture


class Interrupted(BaseException):
    """Model a death that cannot be handled by rollback's Exception handler."""


def _publish_start_generation(fixture, *, change=None):
    """Run the actual signer and consumer publisher for a fresh preparation."""
    core = copy.deepcopy(fixture.result["receipt"])
    core.pop("producer_attestation")
    generation = uuid.uuid4().hex
    core["runtime_publication"]["generation"] = generation
    for relative in activation.RUNTIME_PREPARER_OUTPUTS:
        path = fixture.producer / relative
        activation._atomic(path, path.read_bytes(), 0o600)
        core["runtime_publication"]["outputs"][relative] = publication_fixture._claim(path, relative)
    if change is not None:
        change(core)
    path = fixture.producer / "build-receipt.json"
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        os.ftruncate(descriptor, 0)
        os.write(descriptor, attestation._canonical_json(core))
        os.fsync(descriptor)
        request = attestation.build_request(
            transaction_id=generation, receipt_path=path,
            receipt_descriptor=descriptor, config=fixture.trust,
        )
        response = attestation.RuntimeAttestationDaemon(fixture.trust).attest(
            request, receipt_descriptor=descriptor,
            peer=attestation.PeerCredentials(os.getpid(), os.geteuid(), os.getegid()),
        )
        signed = {**core, "producer_attestation": response}
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, attestation._canonical_json(signed))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fixture.publication.publish(fixture.config)
    observed = fixture.publication.verify(fixture.config)
    fixture.result.update(
        identity=observed["receipt"]["identity"], receipt=signed,
        sha256=observed["producer_receipt_sha256"], publication=observed,
    )
    return generation


@pytest.fixture
def restart_case(consumer_generation, monkeypatch):
    fixture = consumer_generation
    paths = fixture.paths
    for directory in (paths.target, paths.managed_root, paths.managed_bin, paths.unit_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fixture.tx, receipt = activation.new_transaction(paths, source={"commit": "a" * 40})
    preimages, inverse = _inverse_fixture(fixture)

    def broker(**kwargs):
        return fixture.result if kwargs["restore_mode"] == "verify" else inverse

    monkeypatch.setattr(wrappers, "_request_lan_preparation", broker)
    assert activation._restore_tls_generation(
        paths, fixture.tx, {"status": "services-starting"}, preimages,
        checkpoint=lambda _record: None,
    )
    release_id = "1" * 64
    activation._atomic(
        paths.managed_root / "state.json",
        attestation._canonical_json({"current_release_id": release_id, "generation": 7}),
    )
    # Package authenticity has its own integration coverage. Here the release
    # state, all wrappers, units, sources and their exact leaves remain live
    # bindings; only the already-verified wheel execution seam is isolated.
    monkeypatch.setattr(activation, "_release_wrapper_kwargs", lambda _paths, _release: activation._wrapper_kwargs(_paths))
    monkeypatch.setattr(activation, "_preflight_watchdog_wrapper", lambda _paths, _release: {"release_id": _release, "verified": True})
    layout = wrappers.resolve_layout(**activation._wrapper_kwargs(paths))
    for relative, (_source, mode) in activation._sources(paths, fixture.tx).items():
        path = paths.runtime / relative
        if not path.exists():
            activation._atomic(path, b"restored A runtime input\n", mode)
        preimages["runtime:" + relative] = activation._preimage(path, fixture.tx, "source-" + relative.replace("/", "-"))
    for unit in (*activation.SERVICES, activation.WATCHDOG_UNIT):
        path = paths.unit_dir / unit
        spec = wrappers.UNIT_SPEC_BY_NAME[unit]
        activation._atomic(path, ("[Unit]\nDescription=" + unit + "\n[Service]\nType=simple\nExecStart=" + str(layout.wrapper_path(spec, "start")) + "\n").encode(), 0o644)
        preimages["unit:" + unit] = activation._preimage(path, fixture.tx, "unit-" + unit)
    for spec in wrappers.UNIT_SPECS:
        config = {
            "schema": wrappers.UNIT_CONFIG_SCHEMA, "unit": spec.name,
            "release_id": release_id, "state": "stopped", "pid": None,
            "start_ticks": None, "exe": None, "argv": [], "command_sha256": None,
        }
        activation._atomic(layout.config_path(spec), attestation._canonical_json(config))
    for index, path in enumerate(activation._wrapper_publication_paths(paths)):
        if not path.exists():
            activation._atomic(path, b"restored A wrapper\n", 0o600 if path == layout.manifest_path else 0o755)
        preimages["wrapper:" + str(path)] = activation._preimage(path, fixture.tx, "wrapper-" + str(index))

    receipt.update(
        mutation_started=True, old_release_id=release_id,
        services_stopped=True, services_may_be_live=False, preimages=preimages,
        database_before={}, processes_before=[],
        watchdog={"unit": activation.WATCHDOG_UNIT, "disarmed_for_rollback": True},
    )
    activation._persist(paths, fixture.tx, receipt, "rollback-start")
    state = SimpleNamespace(
        fixture=fixture, paths=paths, tx=fixture.tx, receipt=receipt,
        layout=layout, release_id=release_id, children={}, invocations={},
        starts=0, preparations=[], on_start=None, unit_info_changes={}, legacy=False,
        original_command=wrappers.build_start_command,
    )
    command = ("/usr/bin/sleep", "120")
    monkeypatch.setattr(wrappers, "build_start_command", lambda _spec, **_kwargs: command)

    def start(_paths):
        state.starts += 1
        for unit in activation.SERVICES:
            if unit in state.children and state.children[unit].poll() is None:
                continue
            spec = wrappers.UNIT_SPEC_BY_NAME[unit]
            if spec.kind == "lan":
                if state.legacy:
                    legacy = {"schema": "termdeck.lan-runtime-build.v1", "ok": True,
                              **{field: fixture.result["receipt"][field] for field in ("files", "https_port", "lan_ip")}}
                    current = paths.runtime / "current-lan-ip"
                    current.write_bytes(current.read_bytes())
                    hosts = paths.runtime / "nginx/allowed-hosts.map"
                    activation._atomic(hosts, hosts.read_bytes(), 0o600)
                    activation._atomic(paths.runtime / "build-receipt.json", attestation._canonical_json(legacy), 0o644)
                    state.preparations.append("legacy-unsigned")
                else:
                    state.preparations.append(_publish_start_generation(fixture))
            child = subprocess.Popen(command)
            state.children[unit] = child
            state.invocations[unit] = uuid.uuid4().hex
            identity = wrappers.process_identity(child.pid)
            config = {
                "schema": wrappers.UNIT_CONFIG_SCHEMA, "unit": unit,
                "release_id": release_id, "state": "starting", **identity,
                "command_sha256": hashlib.sha256(wrappers._canonical_json(list(command))).hexdigest(),
            }
            if not state.legacy:
                activation._atomic(layout.config_path(spec), attestation._canonical_json(config))
            if spec.kind == "lan":
                activation._atomic(paths.runtime / "nginx/nginx.pid", str(child.pid).encode())
        if state.on_start is not None:
            state.on_start()

    def systemd(*args, **_kwargs):
        if args == ("daemon-reload",):
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[0] == "show", "fixture must never call live systemd"
        unit = args[1]
        child = state.children.get(unit)
        info = {
            "MainPID": str(child.pid if child is not None else 0),
            "InvocationID": state.invocations.get(unit, "0" * 32),
            "ActiveState": "active" if child is not None and child.poll() is None else "inactive",
            "SubState": "running" if child is not None and child.poll() is None else "dead",
            "FragmentPath": str(paths.unit_dir / unit), "DropInPaths": "", "NeedDaemonReload": "no",
        }
        info.update(state.unit_info_changes.get(unit, {}))
        return subprocess.CompletedProcess(args, 0, "\n".join(f"{key}={value}" for key, value in info.items()), "")

    monkeypatch.setattr(activation, "_start_web", start)
    monkeypatch.setattr(activation, "_systemd", systemd)
    monkeypatch.setattr(activation, "_stop_web", lambda: pytest.fail("A is already stopped/restored"))
    monkeypatch.setattr(activation, "_disarm", lambda: pytest.fail("watchdog is already disarmed"))
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"unchanged": True})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"preserved": True})
    try:
        yield state
    finally:
        for child in state.children.values():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def _saved(state):
    return activation.load_transaction(state.paths, state.tx)[1]


def _interrupt_checkpoint(state, monkeypatch, boundary):
    real_checkpoint = activation._checkpoint_publication

    def checkpoint(paths, tx, receipt, record):
        real_checkpoint(paths, tx, receipt, record)
        if record == boundary:
            raise Interrupted(boundary)

    with monkeypatch.context() as patch:
        patch.setattr(activation, "_checkpoint_publication", checkpoint)
        with pytest.raises(Interrupted):
            activation.rollback(state.paths, state.tx, reason="fixture interruption")


def _forbid_completed_inverse(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("completed A inverse must not be repeated during restart recovery")

    for name in ("_recover_pending_release_publications", "_restore_tls_generation", "_restore", "_rollback_package"):
        monkeypatch.setattr(activation, name, unexpected)


@pytest.mark.parametrize("boundary", ["intent:rollback-service-restart", "rollback-service-restart"])
def test_durable_restart_checkpoints_resume_without_repeating_inverse(restart_case, monkeypatch, boundary):
    state = restart_case
    _interrupt_checkpoint(state, monkeypatch, boundary)
    saved = _saved(state)
    restart = saved["rollback_service_restart"]
    assert restart["state"] == ("intent" if boundary.startswith("intent:") else "verified")
    preparations = len(state.preparations)
    _forbid_completed_inverse(monkeypatch)
    result = activation.rollback(state.paths, state.tx, reason="resume")
    assert result["action"] == "rolled-back"
    assert len(state.preparations) == max(1, preparations)
    assert result["receipt"]["rollback_service_restart"]["state"] == "verified"
    assert all(wrappers.verify_process_identity(item["config"]) for item in result["receipt"]["rollback_service_restart"]["observed"]["services"].values())
    calls = state.starts
    assert activation.rollback(state.paths, state.tx, reason="terminal replay")["ok"]
    assert state.starts == calls


def test_interrupted_start_adopts_fresh_signed_generation_and_live_processes(restart_case, monkeypatch):
    state = restart_case
    def interrupt():
        raise Interrupted("prepared and started before outer observation")
    state.on_start = interrupt
    with pytest.raises(Interrupted):
        activation.rollback(state.paths, state.tx, reason="interrupted start")
    assert _saved(state)["rollback_service_restart"]["state"] == "intent"
    generation = state.preparations[0]
    state.on_start = None
    _forbid_completed_inverse(monkeypatch)
    result = activation.rollback(state.paths, state.tx, reason="resume")
    observed = result["receipt"]["rollback_service_restart"]["observed"]
    assert observed["runtime"]["receipt"]["runtime_publication"]["generation"] == generation
    assert observed["runtime"]["receipt"]["tls_publication"]["generation"] != generation
    assert state.preparations == [generation]


def test_postcheck_failure_retries_frozen_restart_without_preparing_again(restart_case, monkeypatch):
    state = restart_case
    with monkeypatch.context() as patch:
        patch.setattr(activation, "verify_database", lambda *_args: (_ for _ in ()).throw(RuntimeError("postcheck interruption")))
        with pytest.raises(RuntimeError, match="postcheck interruption"):
            activation.rollback(state.paths, state.tx, reason="postcheck failure")
    assert _saved(state)["status"] == "rollback-failed"
    assert _saved(state)["rollback_service_restart"]["state"] == "verified"
    assert state.paths.recovery_required.exists()
    _forbid_completed_inverse(monkeypatch)
    calls = state.starts
    assert activation.rollback(state.paths, state.tx, reason="retry")["ok"]
    assert state.starts == calls
    assert not state.paths.recovery_required.exists()


@pytest.mark.parametrize("replacement", ["source", "wrapper", "unit", "release"])
def test_pending_restart_rejects_same_bytes_in_new_bound_input_inode(restart_case, monkeypatch, replacement):
    state = restart_case
    _interrupt_checkpoint(state, monkeypatch, "intent:rollback-service-restart")
    spec = wrappers.UNIT_SPEC_BY_NAME[activation.SERVICES[0]]
    path = {
        "source": state.paths.runtime / "bin/termdeck-web-server",
        "wrapper": state.layout.wrapper_path(spec, "start"),
        "unit": state.paths.unit_dir / activation.SERVICES[0],
        "release": state.paths.managed_root / "state.json",
    }[replacement]
    activation._atomic(path, path.read_bytes(), path.stat().st_mode & 0o777)
    with pytest.raises(activation.WebActivationError, match="binding changed"):
        activation.rollback(state.paths, state.tx, reason="foreign input")
    assert state.starts == 0


@pytest.mark.parametrize("case", ["policy", "signature", "stale", "tls"])
def test_pending_restart_rejects_unrelated_or_unauthenticated_preparation(restart_case, monkeypatch, case):
    state = restart_case
    _interrupt_checkpoint(state, monkeypatch, "intent:rollback-service-restart")
    fixture = state.fixture
    if case == "policy":
        _publish_start_generation(fixture, change=lambda core: core.update(https_port=7444))
    elif case == "signature":
        _publish_start_generation(fixture)
        fixture.result["receipt"]["https_port"] += 1
    elif case == "tls":
        path = fixture.producer / "tls/server.key"
        activation._atomic(path, b"unrelated producer key", 0o600)
        def changed_tls(core):
            claim = publication_fixture._claim(path, path.name)
            claim.pop("path")
            core["tls_publication"]["current"]["manifest"][path.name] = claim
        _publish_start_generation(fixture, change=changed_tls)
    else:
        # Republishing the previous valid producer signature creates new
        # consumer inodes but cannot satisfy the persisted freshness bound.
        fixture.publication.publish(fixture.config)
        observed = fixture.publication.verify(fixture.config)
        fixture.result.update(identity=observed["receipt"]["identity"], publication=observed)
    with pytest.raises(activation.WebActivationError):
        activation.rollback(state.paths, state.tx, reason="unrelated preparation")
    assert state.starts == 0


@pytest.mark.parametrize("replacement", ["signed-publication", "consumer-copy", "config-copy", "service-invocation"])
def test_verified_restart_rejects_later_publication_or_process_replacement(restart_case, monkeypatch, replacement):
    state = restart_case
    _interrupt_checkpoint(state, monkeypatch, "rollback-service-restart")
    if replacement == "signed-publication":
        _publish_start_generation(state.fixture)
    elif replacement in {"consumer-copy", "config-copy"}:
        path = state.paths.runtime / "current-lan-ip" if replacement == "consumer-copy" else state.layout.config_path(wrappers.UNIT_SPEC_BY_NAME[activation.SERVICES[0]])
        activation._atomic(path, path.read_bytes(), path.stat().st_mode & 0o777)
    else:
        state.unit_info_changes[activation.SERVICES[0]] = {"InvocationID": uuid.uuid4().hex}
    calls = state.starts
    with pytest.raises(activation.WebActivationError):
        activation.rollback(state.paths, state.tx, reason="foreign successor")
    assert state.starts == calls


@pytest.mark.parametrize("case", ["wrong-pid", "wrong-argv", "dead", "old-process", "nginx-pid"])
def test_restart_requires_live_expected_service_identity(restart_case, monkeypatch, case):
    state = restart_case
    def invalidate():
        unit = activation.SERVICES[0]
        if case == "wrong-pid":
            state.unit_info_changes[unit] = {"MainPID": "1"}
        elif case == "dead":
            state.children[unit].terminate()
            state.children[unit].wait(timeout=5)
        elif case == "nginx-pid":
            activation._atomic(state.paths.runtime / "nginx/nginx.pid", b"1")
        else:
            path = state.layout.config_path(wrappers.UNIT_SPEC_BY_NAME[unit])
            config = json.loads(path.read_text())
            if case == "old-process":
                config["start_ticks"] = _saved(state)["rollback_service_restart"]["not_before_ticks"] - 1
            else:
                config["argv"] = ["/usr/bin/sleep", "121"]
            activation._atomic(path, attestation._canonical_json(config))
    state.on_start = invalidate
    with pytest.raises(activation.WebActivationError):
        activation.rollback(state.paths, state.tx, reason="incorrect service")
    assert _saved(state)["rollback_service_restart"]["state"] == "intent"


def _select_legacy(state, monkeypatch, *, original_commands=False):
    state.legacy = True
    paths = state.paths
    receipt = state.receipt
    for unit in activation.SERVICES:
        spec = wrappers.UNIT_SPEC_BY_NAME[unit]
        builder = state.original_command if original_commands else wrappers.build_start_command
        command = builder(spec, **activation._wrapper_kwargs(paths))
        if spec.kind == "backend":
            pre = [("/usr/bin/test", "-x", str(paths.runtime / "bin/termdeck-web-server")),
                   ("/usr/bin/test", "-r", str(paths.database))]
            reload = []
        else:
            pre = [(str(paths.runtime / "bin/termdeck-web-prepare"),),
                   ("/usr/bin/nginx", "-t", "-p", str(paths.runtime / "nginx") + "/", "-c", str(paths.runtime / "nginx/nginx.conf"))]
            reload = [("/usr/bin/nginx", "-p", str(paths.runtime / "nginx") + "/", "-c", str(paths.runtime / "nginx/nginx.conf"), "-s", "reload")]
        text = "[Service]\nType=simple\n"
        text += "".join("ExecStartPre=" + shlex.join(argv) + "\n" for argv in pre)
        text += "ExecStart=" + shlex.join(command) + "\n"
        text += "".join("ExecReload=" + shlex.join(argv) + "\n" for argv in reload)
        path = paths.unit_dir / unit
        activation._atomic(path, text.encode(), 0o644)
        receipt["preimages"]["unit:" + unit] = activation._preimage(path, state.tx, "legacy-" + unit)
    # The release manager owns wheel integrity verification; do not install a
    # package or create a production service merely to qualify this handoff.
    monkeypatch.setattr(activation, "verify_installed_tree", lambda path: {"ok": True})
    activation._persist(paths, state.tx, receipt)


def test_original_direct_unit_selection_uses_actual_backend_and_nginx_command_policy(restart_case, monkeypatch):
    state = restart_case
    _select_legacy(state, monkeypatch, original_commands=True)
    monkeypatch.setattr(wrappers, "build_start_command", state.original_command)
    mode, commands = activation._rollback_restart_service_contract(state.paths, state.tx, state.receipt["preimages"])
    assert mode == "legacy-direct"
    assert commands[activation.SERVICES[0]][0] == str(state.paths.runtime / "bin/termdeck-web-server")
    assert commands[activation.SERVICES[1]][0] == "/usr/bin/nginx"
    assert commands[activation.SERVICES[1]][-2:] == ("-g", "daemon off;")


@pytest.mark.parametrize("boundary", ["intent:rollback-service-restart", "rollback-service-restart", "started"])
def test_legacy_original_units_resume_unsigned_startup_without_managed_configs(restart_case, monkeypatch, boundary):
    state = restart_case
    _select_legacy(state, monkeypatch)
    old_inverse = copy.deepcopy(state.receipt["preimages"]["generated:build-receipt.json"])
    if boundary == "started":
        def interrupt():
            raise Interrupted("legacy startup completed before observation")
        state.on_start = interrupt
        with pytest.raises(Interrupted):
            activation.rollback(state.paths, state.tx, reason="legacy interruption")
        state.on_start = None
    else:
        _interrupt_checkpoint(state, monkeypatch, boundary)
    _forbid_completed_inverse(monkeypatch)
    starts = state.starts
    result = activation.rollback(state.paths, state.tx, reason="legacy resume")
    restart = result["receipt"]["rollback_service_restart"]
    assert restart["mode"] == "legacy-direct"
    assert restart["baseline"]["producer_tls"] is None
    assert restart["observed"]["runtime"]["mode"] == "legacy-unsigned-observation"
    assert state.preparations == ["legacy-unsigned"]
    assert state.starts == max(1, starts)
    # Startup observations never become authority to overwrite new unsigned
    # files through A's completed producer inverse or generic file rollback.
    assert result["receipt"]["preimages"]["generated:build-receipt.json"] == old_inverse
    for unit in activation.SERVICES:
        config = json.loads(state.layout.config_path(wrappers.UNIT_SPEC_BY_NAME[unit]).read_text())
        assert config["state"] == "stopped"
        assert "config" not in restart["observed"]["services"][unit]


def test_legacy_pending_changed_unsigned_files_require_completed_expected_processes(restart_case, monkeypatch):
    state = restart_case
    _select_legacy(state, monkeypatch)
    _interrupt_checkpoint(state, monkeypatch, "intent:rollback-service-restart")
    path = state.paths.runtime / "build-receipt.json"
    activation._atomic(path, path.read_bytes(), 0o644)
    with pytest.raises((activation.WebActivationError, wrappers.WrapperError)):
        activation.rollback(state.paths, state.tx, reason="unattributed unsigned file")
    assert state.starts == 0


@pytest.mark.parametrize("change", ["unit-dropin", "wrong-command", "tls-copy", "unsigned-policy", "post-checkpoint-copy"])
def test_legacy_restart_rejects_unexpected_unit_process_tls_or_output(restart_case, monkeypatch, change):
    state = restart_case
    _select_legacy(state, monkeypatch)
    if change == "post-checkpoint-copy":
        _interrupt_checkpoint(state, monkeypatch, "rollback-service-restart")
        path = state.paths.runtime / "build-receipt.json"
        activation._atomic(path, path.read_bytes(), 0o644)
    else:
        def changed_start():
            if change == "unit-dropin":
                state.unit_info_changes[activation.SERVICES[0]] = {"DropInPaths": "/foreign/override.conf"}
            elif change == "wrong-command":
                state.unit_info_changes[activation.SERVICES[0]] = {"MainPID": str(os.getpid())}
            elif change == "tls-copy":
                path = state.paths.runtime / "tls/server.key"
                activation._atomic(path, path.read_bytes(), 0o600)
            else:
                path = state.paths.runtime / "build-receipt.json"
                payload = json.loads(path.read_text())
                payload["https_port"] += 1
                activation._atomic(path, attestation._canonical_json(payload), 0o644)
        state.on_start = changed_start
    with pytest.raises(activation.WebActivationError):
        activation.rollback(state.paths, state.tx, reason="foreign legacy startup")


def test_restart_refuses_missing_watchdog_completion_before_start(restart_case, monkeypatch):
    state = restart_case
    _interrupt_checkpoint(state, monkeypatch, "intent:rollback-service-restart")
    receipt = _saved(state)
    receipt["watchdog"].pop("disarmed_for_rollback")
    activation._persist(state.paths, state.tx, receipt)
    with pytest.raises(activation.WebActivationError, match="disarmed watchdog"):
        activation.rollback(state.paths, state.tx, reason="invalid restart journal")
    assert state.starts == 0


def test_legacy_tls_only_inverse_restarts_original_unsigned_receipt(restart_case, monkeypatch):
    state = restart_case
    fixture = state.fixture
    _select_legacy(state, monkeypatch)
    legacy = {"schema": "termdeck.lan-runtime-build.v1", "ok": True,
              **{field: fixture.result["receipt"][field] for field in ("files", "https_port", "lan_ip")}}
    path = state.paths.runtime / "build-receipt.json"
    activation._atomic(path, attestation._canonical_json(legacy), 0o644)
    record = activation._preimage(path, state.tx, "original-unsigned-A")
    state.receipt["preimages"]["generated:build-receipt.json"] = record
    tls = fixture.result["receipt"]["tls_publication"]["current"]
    generation = "c" * 32
    prior = {"journal": ".termdeck-tls-transaction-" + generation,
             "journal_identity": tls["identity"], "path": "prior-tls",
             "identity": tls["identity"], "manifest": tls["manifest"]}
    record["tls_publication"] = {"generation": generation, "current": tls, "prior": prior}
    producer = {"ok": True, "action": "restored-tls-generation", "generation": generation,
                "receipt": "absent", "restored": tls, "retained": prior}
    consumer = fixture.publication.restore(fixture.config, producer)["result"]
    monkeypatch.setattr(wrappers, "_request_lan_preparation", lambda **_kwargs: {"producer": producer, "publication": consumer})
    assert not activation._restore_tls_generation(
        state.paths, state.tx, {"status": "services-starting"}, state.receipt["preimages"], checkpoint=lambda _: None,
    )
    activation._restore_tombstoned_generated_receipt(record, state.tx)
    activation._persist(state.paths, state.tx, state.receipt)
    _interrupt_checkpoint(state, monkeypatch, "rollback-service-restart")
    _forbid_completed_inverse(monkeypatch)
    result = activation.rollback(state.paths, state.tx, reason="resume original legacy service")
    assert result["ok"]
    assert state.preparations == ["legacy-unsigned"]
    assert json.loads(path.read_text()) == legacy
    assert result["receipt"]["rollback_service_restart"]["mode"] == "legacy-direct"
    assert result["receipt"]["preimages"]["generated:build-receipt.json"]["tls_receipt_tombstoned"]


def test_legacy_restart_does_not_require_nonexistent_original_wrapper_files(restart_case, monkeypatch):
    state = restart_case
    _select_legacy(state, monkeypatch)
    for index, path in enumerate(activation._wrapper_publication_paths(state.paths)):
        path.unlink()
        state.receipt["preimages"]["wrapper:" + str(path)] = activation._preimage(path, state.tx, "absent-wrapper-" + str(index))
    activation._persist(state.paths, state.tx, state.receipt)
    result = activation.rollback(state.paths, state.tx, reason="original unmanaged services")
    assert result["ok"]
    assert result["receipt"]["rollback_service_restart"]["mode"] == "legacy-direct"
    assert all(not path.exists() for path in activation._wrapper_publication_paths(state.paths))


@pytest.mark.parametrize("change", ["command", "extra-preparation", "mixed-modes"])
def test_original_service_contract_refuses_unexpected_legacy_recipe(restart_case, monkeypatch, change):
    state = restart_case
    _select_legacy(state, monkeypatch)
    unit = activation.SERVICES[0]
    spec = wrappers.UNIT_SPEC_BY_NAME[unit]
    path = state.paths.unit_dir / unit
    content = path.read_text()
    if change == "command":
        content = content.replace("ExecStart=/usr/bin/sleep 120", "ExecStart=/usr/bin/sleep 121")
    elif change == "extra-preparation":
        content += "ExecStartPre=/usr/bin/true\n"
    else:
        content = "[Service]\nType=simple\nExecStart=" + str(state.layout.wrapper_path(spec, "start")) + "\n"
    activation._atomic(path, content.encode(), 0o644)
    state.receipt["preimages"]["unit:" + unit] = activation._preimage(path, state.tx, "unexpected-unit")
    activation._persist(state.paths, state.tx, state.receipt)
    with pytest.raises(activation.WebActivationError, match="contract is unsupported|modes are mixed"):
        activation.rollback(state.paths, state.tx, reason="unsupported original recipe")
    assert state.starts == 0
