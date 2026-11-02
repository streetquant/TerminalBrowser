"""Runtime-only A/B rollback with a real signer and disposable broker publisher."""
from __future__ import annotations

import copy
import hashlib
import json
import stat

import pytest

from termdeck import runtime_attestation as attestation
from termdeck import service_wrappers as wrappers
from termdeck import web_activation as activation
from test_web_activation_consumer import consumer_generation
from test_web_activation_rollback_restart import Interrupted, _publish_start_generation


def _postimage(fixture, record):
    path = activation._record_target(record)
    guards = activation._record_parent_guards(record, path)
    _, capture = activation._capture_anchored(path, label="fixture", parent_guards=guards)
    return activation._postimage_payload(path, capture, guards)


@pytest.fixture
def runtime_only(consumer_generation, monkeypatch):
    fixture = consumer_generation
    fixture.old = copy.deepcopy(fixture.result["receipt"])
    fixture.preimages = {
        "generated:" + relative: activation._preimage(fixture.consumer / relative, fixture.tx, "a-" + relative.replace("/", "-"))
        for relative in activation.GENERATED_RUNTIME
    }
    fixture.inputs = ("bin/termdeck-web-server", "nginx/nginx.conf")
    for relative in fixture.inputs:
        path = fixture.consumer / relative
        record = activation._preimage(path, fixture.tx, "source-" + relative.replace("/", "-"))
        fixture.preimages["runtime:" + relative] = record
        activation._atomic(path, path.read_bytes() + b"\nqualified B source\n", record["mode"])
        record["postimage"] = _postimage(fixture, record)
        activation._atomic(fixture.producer / relative, path.read_bytes(), record["mode"])

    def file_hashes(core):
        core["files"] = {relative: hashlib.sha256((fixture.producer / relative).read_bytes()).hexdigest() for relative in core["files"]}

    fixture.file_hashes = file_hashes
    _publish_start_generation(fixture, change=file_hashes)
    fixture.failed = copy.deepcopy(fixture.result["receipt"])
    activation._capture_generated_runtime_postimages(fixture.paths, fixture.tx, fixture.preimages, checkpoint=lambda _: None)
    fixture.record = fixture.preimages["generated:build-receipt.json"]
    fixture.historical = {
        "schema": "termdeck.web-activation-runtime-inverse.v1", "mode": "explicit",
        "generation": fixture.failed["tls_publication"]["generation"],
    }
    fixture.record["runtime_rollback_intent"] = copy.deepcopy(fixture.historical)
    for relative in fixture.inputs:
        record = fixture.preimages["runtime:" + relative]
        record["rollback_restore"] = activation._restore(record, fixture.tx)
    fixture.calls = []
    fixture.before_prepare = None
    fixture.after_prepare = None
    fixture.change_successor = None

    def prepare():
        for relative in fixture.inputs:
            record = fixture.preimages["runtime:" + relative]
            activation._atomic(fixture.producer / relative, (fixture.consumer / relative).read_bytes(), record["mode"])
        def change(core):
            file_hashes(core)
            if fixture.change_successor is not None:
                fixture.change_successor(core)
        _publish_start_generation(fixture, change=change)

    def broker(**kwargs):
        mode = kwargs["restore_mode"]
        fixture.calls.append(mode)
        assert kwargs["runtime_root"] == fixture.consumer
        assert mode in {"none", "verify"}, "must not invoke TLS inverse or real broker"
        if mode == "none":
            if fixture.before_prepare is not None:
                fixture.before_prepare()
            prepare()
            if fixture.after_prepare is not None:
                fixture.after_prepare()
            return None
        return fixture.result

    fixture.prepare = prepare
    monkeypatch.setattr(wrappers, "_request_lan_preparation", broker)
    return fixture


def _restore(fixture, checkpoint=lambda _: None):
    return activation._restore_tls_generation(
        fixture.paths, fixture.tx, {"status": "rollback-failed", "phase_history": ["services-starting"]},
        fixture.preimages, checkpoint=checkpoint,
    )


def _pending(fixture):
    def stop(label):
        assert label == "intent:rollback:runtime-reprepare"
        raise Interrupted()
    with pytest.raises(Interrupted):
        _restore(fixture, stop)
    assert fixture.calls == []
    fixture.preimages = json.loads(json.dumps(fixture.preimages))
    fixture.record = fixture.preimages["generated:build-receipt.json"]


def _tls_incarnations(fixture):
    root = fixture.consumer / "tls"
    info = root.lstat()
    assert stat.S_ISDIR(info.st_mode)

    def identity(observed):
        return (observed.st_dev, observed.st_ino, observed.st_mode,
                observed.st_uid, observed.st_gid, observed.st_nlink)

    leaves = {}
    for path in sorted(root.iterdir()):
        observed = path.lstat()
        assert stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1
        leaves[path.name] = (identity(observed), path.read_bytes())
    return identity(info), leaves


def test_runtime_only_rollback_preserves_tls_historical_intent_and_readonly_repeat(runtime_only):
    f = runtime_only
    checkpoints = []
    old_consumer_tls = copy.deepcopy(f.record["consumer_tls_publication"])
    tls_before = _tls_incarnations(f)
    assert _restore(f, checkpoints.append)
    assert f.calls == ["none", "verify"]
    assert checkpoints == ["intent:rollback:runtime-reprepare", "intent:rollback:runtime-reprepare-request", "rollback:runtime-reprepare"]
    assert f.record["runtime_rollback_intent"] == f.historical
    assert "runtime_rollback" not in f.record
    restored = f.record["runtime_reprepare"]["observed"]["receipt"]
    assert restored["tls_publication"] == f.old["tls_publication"] == f.failed["tls_publication"]
    assert restored["files"] == f.old["files"] != f.failed["files"]
    assert restored["runtime_publication"]["generation"] not in {f.old["runtime_publication"]["generation"], f.failed["runtime_publication"]["generation"]}
    assert f.record["consumer_tls_rollback"]["current"] == old_consumer_tls["current"]
    assert _tls_incarnations(f) == tls_before
    # Consumer identities remain distinct from the signed producer identities.
    assert old_consumer_tls["current"]["identity"] != restored["tls_publication"]["current"]["identity"]
    for relative in activation.GENERATED_RUNTIME:
        record = f.preimages["generated:" + relative]
        assert record["rollback_postimage"] == _postimage(f, record)
        assert record["rollback_restore"] == "restored"
    f.calls.clear()
    before = copy.deepcopy(f.preimages)
    assert _restore(f, lambda _: pytest.fail("completed replay wrote state"))
    assert f.calls == [] and f.preimages == before
    assert _tls_incarnations(f) == tls_before


@pytest.mark.parametrize("boundary", ["intent", "after-publish", "completed"])
def test_runtime_only_rollback_survives_interruption(runtime_only, boundary):
    f = runtime_only
    if boundary == "intent":
        _pending(f)
    elif boundary == "after-publish":
        def death():
            raise Interrupted()
        f.after_prepare = death
        with pytest.raises(Interrupted):
            _restore(f)
        f.after_prepare = None
    else:
        def death(label):
            if label == "rollback:runtime-reprepare":
                raise Interrupted()
        with pytest.raises(Interrupted):
            _restore(f, death)
    f.preimages = json.loads(json.dumps(f.preimages))
    f.record = f.preimages["generated:build-receipt.json"]
    calls_before = f.calls.count("none")
    assert _restore(f)
    assert f.calls.count("none") == calls_before + (boundary == "intent")
    assert f.record["runtime_rollback_intent"] == f.historical


@pytest.mark.parametrize("target", ["current-lan-ip", "nginx/allowed-hosts.map", "build-receipt.json", "tls/server.key", "bin/termdeck-web-server", "nginx/nginx.conf"])
@pytest.mark.parametrize("phase", ["before-intent", "pending", "completed"])
def test_runtime_only_rollback_rejects_foreign_same_content_inode(runtime_only, target, phase):
    f = runtime_only
    if phase == "pending":
        _pending(f)
    elif phase == "completed":
        assert _restore(f)
    f.calls.clear()
    path = f.consumer / target
    activation._atomic(path, path.read_bytes(), path.stat().st_mode & 0o777)
    before = path.stat().st_ino
    with pytest.raises(activation.WebActivationError):
        _restore(f)
    assert "none" not in f.calls
    assert path.stat().st_ino == before


@pytest.mark.parametrize("which", ["A-backup", "B-receipt", "historical-intent", "baseline-TLS", "pending-TLS", "pending-B-signature"])
def test_runtime_only_rollback_rejects_tampered_authority(runtime_only, which):
    f = runtime_only
    if which == "A-backup":
        path = f.tx / f.record["backup"]
        activation._atomic(path, path.read_bytes() + b" ", path.stat().st_mode & 0o777)
    elif which == "B-receipt":
        path = f.consumer / "build-receipt.json"
        value = json.loads(path.read_bytes())
        value["lan_ip"] = "192.168.1.211"
        activation._atomic(path, attestation._canonical_json(value), 0o644)
        f.record["postimage"] = _postimage(f, f.record)
    elif which == "historical-intent":
        f.record["runtime_rollback_intent"]["generation"] = "0" * 32
    elif which == "baseline-TLS":
        f.record["consumer_tls_publication"]["current"]["manifest"]["server.key"]["sha256"] = "0" * 64
    else:
        _pending(f)
        if which == "pending-TLS":
            f.record["tls_publication"]["generation"] = "0" * 32
        else:
            f.record["runtime_reprepare"]["failed_receipt"]["https_port"] += 1
    with pytest.raises(activation.WebActivationError):
        _restore(f)
    assert "none" not in f.calls


@pytest.mark.parametrize("field,value", [("lan_ip", "192.168.1.211"), ("https_port", 9443)])
def test_runtime_only_rollback_rejects_signed_B_policy_change(runtime_only, field, value):
    f = runtime_only
    f.prepare()
    _publish_start_generation(f, change=lambda core: core.update({field: value}))
    for relative in activation.GENERATED_RUNTIME:
        item = f.preimages["generated:" + relative]
        item["postimage"] = _postimage(f, item)
    f.record["consumer_tls_publication"] = activation._consumer_tls_publication(f.result["publication"])
    with pytest.raises(activation.WebActivationError, match="not qualified"):
        _restore(f)
    assert f.calls == []


@pytest.mark.parametrize("change", ["TLS", "policy", "stale", "source-race", "unowned-output", "signed-output"])
def test_runtime_only_rollback_rejects_bad_successor_without_repeating_prepare(runtime_only, change):
    f = runtime_only
    if change == "TLS":
        f.change_successor = lambda core: core["tls_publication"].update(generation="e" * 32)
    elif change == "policy":
        f.change_successor = lambda core: core.update(https_port=9443)
    elif change == "stale":
        _pending(f)
        # Publish a cryptographically valid A successor before its permitted time.
        f.prepare()
        f.record["runtime_reprepare"]["not_before_ns"] = f.result["receipt"]["producer_attestation"]["issued_at_ns"] + 1
    elif change == "source-race":
        def race():
            path = f.consumer / f.inputs[0]
            activation._atomic(path, path.read_bytes(), 0o755)
        f.after_prepare = race
    elif change == "unowned-output":
        def foreign():
            path = f.consumer / "current-lan-ip"
            activation._atomic(path, path.read_bytes(), 0o600)
        f.after_prepare = foreign
    else:
        def different_output():
            activation._atomic(f.producer / "current-lan-ip", b"192.168.1.211\n", 0o600)
        f.before_prepare = different_output
    with pytest.raises(activation.WebActivationError):
        _restore(f)
    calls = f.calls.count("none")
    with pytest.raises(activation.WebActivationError):
        _restore(f)
    assert f.calls.count("none") == calls
    assert f.record["runtime_reprepare"]["state"] == ("intent" if change == "stale" else "preparing")


def test_runtime_only_rollback_checks_A_signature_even_with_consistent_backup_metadata(runtime_only):
    f = runtime_only
    path = f.tx / f.record["backup"]
    altered = json.loads(path.read_bytes())
    altered["https_port"] = 9443
    content = attestation._canonical_json(altered)
    activation._atomic(path, content, f.record["mode"])
    guards = activation._parent_guards_from_payload(f.record["backup_postimage"]["parent_guard"], path)
    _, capture = activation._capture_anchored(path, label="tampered fixture backup", parent_guards=guards)
    f.record.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content),
                    backup_postimage=activation._postimage_payload(path, capture, guards))
    with pytest.raises(activation.WebActivationError, match="attestation"):
        _restore(f)
    assert f.calls == []


def test_runtime_only_rollback_fences_source_after_durable_intent(runtime_only):
    f = runtime_only
    def race(label):
        assert label == "intent:rollback:runtime-reprepare"
        path = f.consumer / f.inputs[0]
        activation._atomic(path, path.read_bytes(), 0o755)
    with pytest.raises(activation.WebActivationError):
        _restore(f, race)
    assert f.calls == []
    assert f.record["runtime_reprepare"]["state"] == "intent"


def test_runtime_only_rollback_refuses_uncertain_submission_before_broker_call(runtime_only):
    f = runtime_only
    def death(label):
        if label == "intent:rollback:runtime-reprepare-request":
            raise Interrupted()
    with pytest.raises(Interrupted):
        _restore(f, death)
    assert f.calls == []
    f.preimages = json.loads(json.dumps(f.preimages))
    f.record = f.preimages["generated:build-receipt.json"]
    with pytest.raises(activation.WebActivationError, match="outcome is uncertain"):
        _restore(f)
    assert f.calls == []


def test_runtime_only_rollback_never_repeats_producer_complete_consumer_baseline(runtime_only, monkeypatch):
    f = runtime_only
    baseline = {relative: (f.consumer / relative).stat().st_ino for relative in activation.GENERATED_RUNTIME}
    def refuse_publication(_config):
        # The real signing helper has already published the producer's signed
        # A receipt; consumer publication fails before touching its B leaves.
        raise wrappers.WrapperError("disposable consumer publication refused")
    monkeypatch.setattr(f.publication, "publish", refuse_publication)
    with pytest.raises(activation.WebActivationError, match="broker preparation failed"):
        _restore(f)
    producer_receipt = json.loads((f.producer / "build-receipt.json").read_bytes())
    attestation.verify_runtime_build_attestation(producer_receipt, config=f.trust)
    assert producer_receipt["files"] == f.old["files"]
    assert {relative: (f.consumer / relative).stat().st_ino for relative in activation.GENERATED_RUNTIME} == baseline
    assert f.record["runtime_reprepare"]["state"] == "preparing"
    f.preimages = json.loads(json.dumps(f.preimages))
    f.record = f.preimages["generated:build-receipt.json"]
    with pytest.raises(activation.WebActivationError, match="outcome is uncertain"):
        _restore(f)
    assert f.calls == ["none"]
    assert json.loads((f.producer / "build-receipt.json").read_bytes()) == producer_receipt
