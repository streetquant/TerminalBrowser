"""Exercise signed producer receipts with genuinely different consumer inodes."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from termdeck import runtime_attestation as attestation
from termdeck import service_wrappers as wrappers
from termdeck import web_activation as activation
import test_termdeck_preparer_publication as publication_fixture
import test_web_activation as activation_fixture


@pytest.fixture
def consumer_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    publication = publication_fixture.publication
    config, producer, consumer = publication_fixture.layout.__wrapped__(tmp_path)
    publication.seed(config)
    unsigned = publication_fixture._prepare_generation(producer)
    producer_paths = dataclasses.replace(activation_fixture._paths(tmp_path), runtime=producer)
    trust = activation_fixture._prepare_test_attestor(producer_paths)
    core = {key: value for key, value in unsigned.items()
            if key not in {"producer_attestation", "producer_commitment"}}
    # TLS rotation and runtime publication are independent signed transactions.
    core["tls_publication"]["generation"] = "a" * 32
    receipt_path = producer / "build-receipt.json"
    descriptor = os.open(receipt_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        os.ftruncate(descriptor, 0)
        os.write(descriptor, attestation._canonical_json(core))
        os.fsync(descriptor)
        request = attestation.build_request(
            transaction_id=core["runtime_publication"]["generation"],
            receipt_path=receipt_path, receipt_descriptor=descriptor, config=trust,
        )
        response = attestation.RuntimeAttestationDaemon(trust).attest(
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

    paths = dataclasses.replace(producer_paths, runtime=consumer)
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    preimages = {
        "generated:" + relative: activation._preimage(consumer / relative, tx, str(index))
        for index, relative in enumerate(activation.GENERATED_RUNTIME)
    }
    publication.publish(config)
    observed = publication.verify(config)
    result = {
        "identity": observed["receipt"]["identity"],
        "sha256": observed["producer_receipt_sha256"],
        "receipt": signed,
        "publication": observed,
    }
    monkeypatch.setattr(wrappers, "CANONICAL_RUNTIME_ROOT", consumer)
    monkeypatch.setattr(wrappers, "PREPARER_RUNTIME_ROOT", consumer)
    monkeypatch.setattr(attestation, "production_config", lambda: trust)
    monkeypatch.setattr(wrappers, "_request_lan_preparation", lambda **_kwargs: result)
    return SimpleNamespace(
        paths=paths, tx=tx, preimages=preimages, result=result, producer=producer,
        consumer=consumer, config=config, publication=publication, trust=trust,
    )


def test_consumer_adoption_preserves_signed_producer_and_records_consumer(consumer_generation):
    fixture = consumer_generation
    signed_before = attestation._canonical_json(fixture.result["receipt"])
    checkpoints = []
    activation._capture_generated_runtime_postimages(
        fixture.paths, fixture.tx, fixture.preimages, checkpoint=checkpoints.append,
    )
    for relative in activation.GENERATED_RUNTIME:
        record = fixture.preimages["generated:" + relative]
        assert record["postimage"]["identity"]["inode"] == (fixture.consumer / relative).stat().st_ino
        assert record["postimage"]["identity"]["inode"] != (fixture.producer / relative).stat().st_ino
    record = fixture.preimages["generated:build-receipt.json"]
    assert record["tls_publication"] == fixture.result["receipt"]["tls_publication"]
    assert record["consumer_tls_publication"]["current"]["identity"][1] == (fixture.consumer / "tls").stat().st_ino
    assert record["consumer_tls_publication"]["current"]["identity"] != record["tls_publication"]["current"]["identity"]
    assert attestation._canonical_json(fixture.result["receipt"]) == signed_before
    activation._capture_generated_runtime_postimages(
        fixture.paths, fixture.tx, fixture.preimages, checkpoint=lambda _: pytest.fail("replay checkpoint"),
    )


@pytest.mark.parametrize("case", [
    "legacy-envelope", "receipt-digest", "receipt-size", "receipt-identity",
    "generation", "output-digest", "output-size", "tls-digest", "tls-size",
    "tls-owner", "extra-leaf", "signed-tamper", "wrong-operation",
])
def test_consumer_receipt_rejects_unbound_or_malformed_claims(consumer_generation, case):
    fixture = consumer_generation
    result = fixture.result
    publication = result["publication"]
    if case == "legacy-envelope":
        result.pop("publication")
    elif case == "receipt-digest":
        result["sha256"] = "0" * 64
    elif case == "receipt-size":
        result["identity"][4] += 1
    elif case == "receipt-identity":
        result["identity"] = list(result["identity"])
        result["identity"][1] += 1
    elif case == "generation":
        publication["generation"] = "f" * 32
    elif case.startswith("output-"):
        claim = publication["outputs"]["current-lan-ip"]
        if case.endswith("digest"):
            claim["sha256"] = "0" * 64
        else:
            claim["identity"][4] += 1
    elif case in {"tls-digest", "tls-size"}:
        claim = publication["tls"]["manifest"]["server.key"]
        if case.endswith("digest"):
            claim["sha256"] = "0" * 64
        else:
            claim["identity"][4] += 1
    elif case == "tls-owner":
        publication["tls"]["identity"][3] += 1
    elif case == "extra-leaf":
        publication["tls"]["manifest"]["foreign"] = {}
    elif case == "signed-tamper":
        result["receipt"]["https_port"] += 1
        payload = attestation._canonical_json(result["receipt"])
        result["sha256"] = publication["producer_receipt_sha256"] = publication["receipt"]["sha256"] = hashlib.sha256(payload).hexdigest()
        result["identity"][4] = len(payload)
    elif case == "wrong-operation":
        publication["operation"] = "publish"
    with pytest.raises(activation.WebActivationError):
        activation._capture_generated_runtime_postimages(
            fixture.paths, fixture.tx, fixture.preimages,
            checkpoint=lambda _: pytest.fail("invalid observation adopted"),
        )
    assert all(record["postimage"] is None for record in fixture.preimages.values())


@pytest.mark.parametrize("relative", ["current-lan-ip", "build-receipt.json", "tls/server.key"])
def test_consumer_replacement_after_broker_observation_is_rejected(consumer_generation, relative):
    fixture = consumer_generation
    path = fixture.consumer / relative
    activation._atomic(path, path.read_bytes(), path.stat().st_mode & 0o777)
    with pytest.raises(activation.WebActivationError):
        activation._capture_generated_runtime_postimages(
            fixture.paths, fixture.tx, fixture.preimages,
            checkpoint=lambda _: pytest.fail("foreign consumer adopted"),
        )


def test_no_change_uses_consumer_identities(consumer_generation):
    fixture = consumer_generation
    preimages = {
        "generated:" + relative: activation._preimage(fixture.consumer / relative, fixture.tx, "new" + str(index))
        for index, relative in enumerate(activation.GENERATED_RUNTIME)
    }
    assert activation._runtime_inverse_has_no_changes(fixture.paths, fixture.tx, preimages)
    foreign = fixture.result["publication"]["outputs"]["current-lan-ip"]["identity"]
    foreign[1] += 1
    assert not activation._runtime_inverse_has_no_changes(fixture.paths, fixture.tx, preimages)


def test_failed_current_verification_still_discovers_authenticated_inverse(consumer_generation, monkeypatch):
    fixture = consumer_generation
    preimages, envelope = _inverse_fixture(fixture)
    calls = []

    def request(**kwargs):
        mode = kwargs["restore_mode"]
        calls.append(mode)
        if calls == ["verify"]:
            raise wrappers.WrapperError("current generation is incomplete")
        return fixture.result if mode == "verify" else envelope

    monkeypatch.setattr(wrappers, "_request_lan_preparation", request)
    checkpoints = []
    assert activation._restore_tls_generation(
        fixture.paths, fixture.tx, {"status": "services-starting"}, preimages,
        checkpoint=checkpoints.append,
    )
    assert calls == ["verify", "unreceipted", "verify"]
    assert checkpoints[0] == "intent:rollback:generated-runtime"


def test_failed_verification_and_failed_discovery_never_claim_recovery(consumer_generation, monkeypatch):
    fixture = consumer_generation
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs["restore_mode"])
        raise wrappers.WrapperError("broker unavailable")

    monkeypatch.setattr(wrappers, "_request_lan_preparation", unavailable)
    with pytest.raises(activation.WebActivationError):
        activation._restore_tls_generation(
            fixture.paths, fixture.tx, {"status": "services-starting"}, fixture.preimages,
            checkpoint=lambda _: None,
        )
    assert calls == ["verify", "unreceipted"]
    assert "runtime_rollback" not in fixture.preimages["generated:build-receipt.json"]


def _reissue_receipt(fixture):
    core = copy.deepcopy(fixture.result["receipt"])
    core.pop("producer_attestation")
    core["runtime_publication"]["generation"] = "f" * 32
    path = fixture.producer / "build-receipt.json"
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        os.ftruncate(descriptor, 0)
        os.write(descriptor, attestation._canonical_json(core))
        os.fsync(descriptor)
        request = attestation.build_request(
            transaction_id="f" * 32, receipt_path=path,
            receipt_descriptor=descriptor, config=fixture.trust,
        )
        signed = {**core, "producer_attestation": attestation.RuntimeAttestationDaemon(fixture.trust).attest(
            request, receipt_descriptor=descriptor,
            peer=attestation.PeerCredentials(os.getpid(), os.geteuid(), os.getegid()),
        )}
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, attestation._canonical_json(signed))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return signed


def _inverse_fixture(fixture):
    preimages = {
        "generated:" + relative: activation._preimage(fixture.consumer / relative, fixture.tx, "prior" + str(index))
        for index, relative in enumerate(activation.GENERATED_RUNTIME)
    }
    signed = _reissue_receipt(fixture)
    tls = signed["tls_publication"]["current"]
    producer = {
        "ok": True, "action": "restored-runtime-generation", "generation": "b" * 32,
        "discovered": True,
        "receipt": {key: value for key, value in publication_fixture._claim(fixture.producer / "build-receipt.json", "build-receipt.json").items() if key != "mode"},
        "restored": {
            "outputs": {relative: {key: claim[key] for key in ("identity", "sha256")}
                        for relative, claim in signed["runtime_publication"]["outputs"].items()},
            "runtime_generation": signed["runtime_publication"]["generation"], "tls": tls,
        },
        "retained": {
            "journal": ".termdeck-tls-transaction-" + "b" * 32,
            "journal_identity": tls["identity"], "path": "prior-tls",
            "identity": tls["identity"], "manifest": tls["manifest"],
        },
    }
    consumer = fixture.publication.restore(fixture.config, producer)["result"]
    envelope = {"producer": producer, "publication": consumer}
    publication = fixture.publication.verify(fixture.config)
    fixture.result.update(identity=publication["receipt"]["identity"], receipt=signed,
                          sha256=publication["receipt"]["sha256"], publication=publication)
    return preimages, envelope


def test_full_inverse_checks_fresh_signature_consumer_and_reissued_generation(consumer_generation, monkeypatch):
    fixture = consumer_generation
    preimages, envelope = _inverse_fixture(fixture)
    calls = []
    def request(**kwargs):
        calls.append(kwargs["restore_mode"])
        return fixture.result if kwargs["restore_mode"] == "verify" else envelope
    monkeypatch.setattr(wrappers, "_request_lan_preparation", request)
    receipt = {"status": "services-starting"}
    checkpoints = []
    assert activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, preimages, checkpoint=checkpoints.append)
    assert "unreceipted" in calls
    assert checkpoints[-1] == "rollback:generated-runtime"
    for relative in activation.GENERATED_RUNTIME:
        assert preimages["generated:" + relative]["rollback_postimage"]["identity"]["inode"] == (fixture.consumer / relative).stat().st_ino
    calls.clear()
    assert activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, preimages, checkpoint=lambda _: pytest.fail("replay changed durable inverse"))
    assert calls == ["verify"]
    path = fixture.consumer / "current-lan-ip"
    activation._atomic(path, path.read_bytes(), 0o600)
    with pytest.raises(activation.WebActivationError):
        activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, preimages, checkpoint=lambda _: pytest.fail("foreign inverse adopted"))


@pytest.mark.parametrize("case", ["old-envelope", "lineage", "output-digest", "warnings", "unbound-receipt"])
def test_inverse_rejects_malformed_or_unbound_consumer_envelope(consumer_generation, case):
    fixture = consumer_generation
    _preimages, envelope = _inverse_fixture(fixture)
    if case == "old-envelope":
        envelope = envelope["publication"]
    elif case == "lineage":
        envelope["publication"] = copy.deepcopy(envelope["publication"])
        envelope["publication"]["restored"]["runtime_generation"] = "e" * 32
    elif case == "output-digest":
        envelope["publication"]["restored"]["outputs"]["current-lan-ip"]["sha256"] = "0" * 64
    elif case == "warnings":
        envelope["publication"]["consumer_cleanup_warnings"] = ["x" * 513]
    elif case == "unbound-receipt":
        envelope["publication"]["receipt"]["path"] = "/foreign"
    with pytest.raises(activation.WebActivationError):
        activation._unwrap_broker_inverse(envelope)


def test_tls_only_inverse_preserves_consumer_identity_on_replay(consumer_generation, monkeypatch):
    fixture = consumer_generation
    tls = fixture.result["receipt"]["tls_publication"]["current"]
    generation = "b" * 32
    prior = {"journal": ".termdeck-tls-transaction-" + generation,
             "journal_identity": tls["identity"], "path": "prior-tls",
             "identity": tls["identity"], "manifest": tls["manifest"]}
    record = fixture.preimages["generated:build-receipt.json"]
    record["tls_publication"] = {"generation": generation, "current": tls, "prior": prior}
    producer = {"ok": True, "action": "restored-tls-generation", "generation": generation,
                "receipt": "absent", "restored": tls, "retained": prior}
    consumer = fixture.publication.restore(fixture.config, producer)["result"]
    monkeypatch.setattr(wrappers, "_request_lan_preparation", lambda **kwargs: {"producer": producer, "publication": consumer})
    receipt = {"status": "services-starting"}
    checkpoints = []
    assert not activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, fixture.preimages, checkpoint=checkpoints.append)
    assert record["tls_receipt_tombstoned"]
    assert not (fixture.consumer / "build-receipt.json").exists()
    inode = (fixture.consumer / "tls").stat().st_ino
    monkeypatch.setattr(wrappers, "_request_lan_preparation", lambda **kwargs: pytest.fail("completed TLS inverse republished"))
    assert not activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, fixture.preimages, checkpoint=lambda _: pytest.fail("replay checkpoint"))
    assert (fixture.consumer / "tls").stat().st_ino == inode
    # The outer inverse may restore a legacy receipt after the TLS tombstone.
    activation._restore_tombstoned_generated_receipt(record, fixture.tx)
    assert activation._restore_tombstoned_generated_receipt(record, fixture.tx) == "already-restored"
    assert not activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, fixture.preimages, checkpoint=lambda _: pytest.fail("replay checkpoint"))
    path = fixture.consumer / "tls/server.key"
    activation._atomic(path, path.read_bytes(), 0o600)
    with pytest.raises(activation.WebActivationError):
        activation._restore_tls_generation(fixture.paths, fixture.tx, receipt, fixture.preimages, checkpoint=lambda _: pytest.fail("foreign TLS accepted"))


@pytest.mark.parametrize("phase", ["prepared", "journal-intent", "before-postimage", "postimage-recorded", "committed"])
def test_tombstoned_receipt_restore_survives_hard_death(tmp_path, phase):
    target = tmp_path / "runtime/build-receipt.json"
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    activation._atomic(target, b"legacy authenticated A receipt", 0o644)
    record = activation._preimage(target, tx, "receipt")
    target.unlink()  # Disposable fixture representing the authenticated TLS inverse.
    record["tls_receipt_tombstoned"] = True
    saved = tx / "record.json"
    activation._write(saved, record)
    child = r'''
import os, sys
from pathlib import Path
from termdeck import web_activation as activation
tx, phase = Path(sys.argv[1]), sys.argv[2]
saved = tx / "record.json"
record = activation._read(saved)
original = activation._publish_anchored
def publish(*args, **kwargs):
    if phase == "before-postimage" and kwargs.get("postimage_checkpoint"):
        kwargs["postimage_checkpoint"] = lambda _: os._exit(89)
    return original(*args, **kwargs)
activation._publish_anchored = publish
def checkpoint():
    activation._write(saved, record)
    if record.get("tombstone_publication", {}).get("publication_intent", {}).get("state") == phase:
        os._exit(89)
activation._restore_tombstoned_generated_receipt(record, tx, checkpoint=checkpoint)
if phase == "committed":
    os._exit(89)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(tx), phase],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == 89, crashed.stderr
    record = activation._read(saved)
    if phase in {"journal-intent", "before-postimage", "postimage-recorded"}:
        assert activation._publication_journal_path(target, tx).exists()
    # TLS replay encounters this checkpoint before the outer receipt inverse.
    activation._recover_tombstoned_receipt_publication(
        record, tx, checkpoint=lambda: activation._write(saved, record),
    )
    activation._verify_consumer_receipt_tombstone(record, replay=True)
    activation._restore_tombstoned_generated_receipt(
        record, tx, checkpoint=lambda: activation._write(saved, record),
    )
    assert target.read_bytes() == b"legacy authenticated A receipt"
    assert target.stat().st_mode & 0o777 == 0o644
    assert not activation._publication_journal_path(target, tx).exists()
    assert activation._restore_tombstoned_generated_receipt(record, tx) == "already-restored"
    activation._atomic(target, target.read_bytes(), 0o644)
    with pytest.raises(activation.WebActivationError):
        activation._restore_tombstoned_generated_receipt(record, tx)


@pytest.mark.parametrize("case", ["target", "digest", "mode", "journal", "not-tombstoned"])
def test_tombstoned_receipt_rejects_unbound_publication(tmp_path, case):
    target = tmp_path / "runtime/build-receipt.json"
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    activation._atomic(target, b"original A", 0o644)
    record = activation._preimage(target, tx, "receipt")
    target.unlink()
    record["tls_receipt_tombstoned"] = True
    activation._restore_tombstoned_generated_receipt(record, tx)
    nested = record["tombstone_publication"]
    if case == "target":
        nested["path"] = str(target.parent / "foreign.json")
    elif case == "digest":
        nested["publication_intent"]["sha256"] = "0" * 64
    elif case == "mode":
        nested["publication_intent"]["mode"] = 0o600
    elif case == "journal":
        nested["publication_intent"]["journal"] = str(target.parent / "foreign-journal")
    else:
        record["tls_receipt_tombstoned"] = False
    inode = target.stat().st_ino
    with pytest.raises(activation.WebActivationError):
        activation._restore_tombstoned_generated_receipt(record, tx)
    assert target.stat().st_ino == inode
    assert target.read_bytes() == b"original A"
