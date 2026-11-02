"""Forward recovery preserves failure evidence and rejects stale admission."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from termdeck import forward_recovery as recovery
from termdeck import web_activation as activation
from termdeck import service_wrappers
from termdeck import release_manager
from test_web_activation import _paths, _release_artifact, _integration_paths, _storage_attestation, _inject_post_install_failure
from test_web_activation_consumer import consumer_generation


@pytest.fixture
def transition(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    source = {"commit": "a" * 40, "tree": "b" * 40}
    old, failed = activation.new_transaction(paths, source)
    failed.update(mutation_started=True)
    activation._persist(paths, old, failed, "rollback-failed")
    activation._atomic(paths.recovery_required, (str(old) + "\n").encode())
    digest = activation._sha(old / "receipt.json")
    original = (old / "receipt.json").read_bytes()
    tx, receipt = activation.new_transaction(paths, source)
    activation._persist(paths, tx, receipt, "artifacts-staged")
    candidate = {"transaction": str(tx), "package": {"release_id": "new"}}
    baseline = {"state": {"release_id": "old"},
                "tls": {"consumer_tls": {"generation": "current"}}, "services": {"pid": 123}}
    monkeypatch.setattr(recovery, "_candidate", lambda *_: copy.deepcopy(candidate))
    monkeypatch.setattr(recovery, "_baseline", lambda *_: copy.deepcopy(baseline))
    return SimpleNamespace(paths=paths, old=old, tx=tx, digest=digest, original=original,
                           baseline=baseline, candidate=candidate)


def _prepare(f):
    recovery.stabilize(f.paths, f.tx, f.old, f.digest)
    return activation.load_transaction(f.paths, f.tx)[1]


def _activated(f):
    receipt = _prepare(f)
    receipt["package"] = {"release_id": "new"}
    activation._persist(f.paths, f.tx, receipt, "activated")
    f.baseline["state"]["release_id"] = "new"
    return receipt


def test_preparation_and_repeated_admission_preserve_failed_evidence(transition):
    f = transition
    marker = f.paths.recovery_required.read_bytes()
    receipt = _prepare(f)
    recovery.admit(f.paths, f.tx, receipt)
    recovery.admit(f.paths, f.tx, receipt)
    assert (f.old / "receipt.json").read_bytes() == f.original
    assert f.paths.recovery_required.read_bytes() == marker
    assert receipt["status"] == "artifacts-staged"
    assert receipt["mutation_started"] is False


def test_unreviewed_execution_stops_before_broker_or_recovery_mutations(transition, monkeypatch):
    f = transition
    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation, "_require_preserved_tls_preparation", lambda *_: pytest.fail("broker reached"))
    with pytest.raises(activation.WebActivationError, match="requires reviewed --stabilize"):
        activation.execute(f.paths, f.tx)
    assert activation.load_transaction(f.paths, f.tx)[1]["mutation_started"] is False


@pytest.mark.parametrize("change", ["failed-bytes", "failed-inode", "marker-bytes", "marker-inode",
                                   "candidate", "release", "tls", "service", "expired", "future"])
def test_stale_admission_rejected_before_mutation(transition, monkeypatch, change):
    f = transition
    receipt = _prepare(f)
    if change == "failed-bytes":
        with (f.old / "receipt.json").open("ab") as out:
            out.write(b"\n")
    elif change == "failed-inode":
        activation._atomic(f.old / "receipt.json", f.original)
    elif change == "marker-bytes":
        activation._atomic(f.paths.recovery_required, b"foreign\n")
    elif change == "marker-inode":
        activation._atomic(f.paths.recovery_required, f.paths.recovery_required.read_bytes())
    elif change == "candidate":
        f.candidate["package"]["release_id"] = "foreign"
    elif change == "release":
        f.baseline["state"]["release_id"] = "foreign"
    elif change == "tls":
        f.baseline["tls"]["consumer_tls"]["generation"] = "foreign"
    elif change == "service":
        f.baseline["services"]["pid"] += 1
    else:
        monkeypatch.setattr(recovery.time, "time", lambda: receipt["forward_recovery"]["prepared_epoch"] + (601 if change == "expired" else -1))
    with pytest.raises(activation.WebActivationError):
        recovery.admit(f.paths, f.tx, receipt)
    assert activation.load_transaction(f.paths, f.tx)[1]["mutation_started"] is False


def test_failed_transaction_cannot_be_its_own_successor(transition):
    f = transition
    with pytest.raises(activation.WebActivationError):
        recovery.stabilize(f.paths, f.old, f.old, f.digest)


@pytest.mark.parametrize("field,value", [("runtime", "/foreign"), ("status", "activated"),
                                        ("mutation_started", False), ("services", [])])
def test_matching_digest_does_not_authorize_foreign_boundary(transition, field, value):
    f = transition
    old = json.loads(f.original)
    old[field] = value
    activation._write(f.old / "receipt.json", old)
    with pytest.raises(activation.WebActivationError, match="different recovery boundary"):
        recovery.stabilize(f.paths, f.tx, f.old, activation._sha(f.old / "receipt.json"))


def test_discharge_requires_success_and_preserves_old_failed_receipt(transition):
    f = transition
    receipt = _prepare(f)
    with pytest.raises(activation.WebActivationError, match="only durable activation"):
        recovery.complete(f.paths, f.tx, receipt)
    receipt["package"] = {"release_id": "new"}
    activation._persist(f.paths, f.tx, receipt, "activated")
    f.baseline["state"]["release_id"] = "new"
    recovery.complete(f.paths, f.tx, receipt)
    assert not f.paths.recovery_required.exists()
    assert (f.old / "receipt.json").read_bytes() == f.original
    assert json.loads(f.original)["status"] == "rollback-failed"
    recovery.complete(f.paths, f.tx, activation.load_transaction(f.paths, f.tx)[1])


@pytest.mark.parametrize("change", ["marker", "tls", "release", "failure"])
def test_discharge_drift_keeps_gate(transition, change):
    f = transition
    receipt = _activated(f)
    if change == "marker":
        activation._atomic(f.paths.recovery_required, b"foreign\n")
    elif change == "tls":
        f.baseline["tls"]["consumer_tls"]["generation"] = "foreign"
    elif change == "release":
        f.baseline["state"]["release_id"] = "foreign"
    else:
        activation._atomic(f.old / "receipt.json", f.original + b"\n")
    with pytest.raises(activation.WebActivationError):
        recovery.complete(f.paths, f.tx, receipt)
    assert f.paths.recovery_required.exists()
    assert activation.load_transaction(f.paths, f.tx)[1]["status"] == "activated"


@pytest.mark.parametrize("crash_after_delete", [False, True])
def test_crash_at_gate_discharge_is_resumable(transition, monkeypatch, crash_after_delete):
    f = transition
    receipt = _activated(f)
    publish = activation._publish_anchored
    class Crash(BaseException):
        pass
    def interrupt(path, *args, **kwargs):
        if path != f.paths.recovery_required:
            return publish(path, *args, **kwargs)
        if crash_after_delete:
            publish(path, *args, **kwargs)
        raise Crash()
    with monkeypatch.context() as scoped:
        scoped.setattr(activation, "_publish_anchored", interrupt)
        with pytest.raises(Crash):
            recovery.complete(f.paths, f.tx, receipt)
    reloaded = activation.load_transaction(f.paths, f.tx)[1]
    assert reloaded["forward_recovery"]["completion"] == "discharge-prepared"
    recovery.complete(f.paths, f.tx, reloaded)
    assert not f.paths.recovery_required.exists()
    assert (f.old / "receipt.json").read_bytes() == f.original


def test_foreign_marker_between_read_and_cas_is_preserved(transition, monkeypatch):
    f = transition
    receipt = _activated(f)
    persist = activation._persist
    def race(paths, tx, value, *args):
        persist(paths, tx, value, *args)
        if value["forward_recovery"].get("completion") == "discharge-prepared":
            activation._atomic(paths.recovery_required, b"newer recovery owner\n")
    monkeypatch.setattr(activation, "_persist", race)
    with pytest.raises(activation.WebActivationError):
        recovery.complete(f.paths, f.tx, receipt)
    assert f.paths.recovery_required.read_bytes() == b"newer recovery owner\n"


def test_tls_baseline_uses_real_signature_and_separate_consumer_inodes(consumer_generation, monkeypatch):
    f = consumer_generation
    monkeypatch.setattr(activation, "_observed_primary_lan_ipv4", lambda *_: f.result["receipt"]["lan_ip"])
    monkeypatch.setattr(activation, "_installed_tls_reuse_publisher_sha256", lambda *_: "a" * 64)
    monkeypatch.setattr(activation, "_run", lambda *_args, **_kwargs: None)  # fixture certificates are synthetic bytes
    monkeypatch.setattr(service_wrappers, "_request_lan_preparation", lambda **_: pytest.fail("must not seed private producer"))
    result = recovery._tls(f.paths)
    assert result["consumer_tls"]["generation"] == f.result["receipt"]["tls_publication"]["generation"]
    assert result["consumer_tls"]["generation"] != f.result["receipt"]["runtime_publication"]["generation"]
    assert result["consumer_tls"]["current"]["identity"][1] == (f.consumer / "tls").stat().st_ino
    assert result["consumer_tls"]["current"]["identity"] != f.result["receipt"]["tls_publication"]["current"]["identity"]
    receipt_path = f.consumer / "build-receipt.json"
    forged = json.loads(receipt_path.read_bytes())
    forged["lan_ip"] = "192.168.68.61"
    activation._write(receipt_path, forged, 0o644)
    with pytest.raises(activation.WebActivationError, match="signature"):
        recovery._tls(f.paths)


def test_candidate_checks_real_wheel_and_all_staged_files(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    source = {"commit": "a" * 40, "tree": "b" * 40}
    tx, receipt = activation.new_transaction(paths, source)
    wheel, manifest, info = _release_artifact(tx / "artifacts" / "package", "c")
    import shutil
    package_dir = tx / "artifacts" / "package"
    wheel = Path(shutil.copy2(wheel, package_dir / wheel.name))
    manifest = Path(shutil.copy2(manifest, package_dir / manifest.name))
    receipt["package"] = {"wheel": str(wheel), "manifest": str(manifest),
                          "release_id": info["release_id"], "wheel_sha256": activation._sha(wheel)}
    for relative in activation._sources(paths, tx):
        activation._atomic(tx / "staged-runtime" / relative, b"qualified", 0o755)
    for unit in (*activation.SERVICES, activation.WATCHDOG_UNIT):
        activation._atomic(tx / "staged-units" / unit, b"qualified unit", 0o644)
    receipt["artifacts"] = {"files": {str(p.relative_to(tx)): activation._fingerprint(p)
        for base in (tx / "artifacts", tx / "staged-runtime", tx / "staged-units")
        for p in base.rglob("*") if p.is_file()}}
    monkeypatch.setattr(activation, "_source", lambda *_: source)
    recovery._candidate(paths, tx, receipt)
    (tx / "staged-runtime/nginx/nginx.conf").write_bytes(b"foreign")
    with pytest.raises(activation.WebActivationError, match="artifact changed"):
        recovery._candidate(paths, tx, receipt)


@pytest.mark.parametrize("failure", [False, True])
def test_real_package_forward_recovery_and_own_inverse_preserve_old_failure(tmp_path, monkeypatch, failure):
    """Real wheels, release/wrapper journals and inverse; no host systemd calls."""
    import shutil
    import sys
    paths = _integration_paths(tmp_path)
    wheel_a, manifest_a, release_a = _release_artifact(tmp_path, "a")
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    monkeypatch.setattr(release_manager, "_require_managed_storage", lambda *_: _storage_attestation())
    release_manager.install_release(wheel_a, manifest_a, root=paths.managed_root,
        bin_dir=paths.managed_bin, python=sys.executable, production=True)
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777)
              for path in activation._wrapper_publication_paths(paths)}
    source = {"commit": "c" * 40, "tree": "d" * 40}
    monkeypatch.setattr(activation, "_source", lambda *_: source)
    old, old_receipt = activation.new_transaction(paths, source)
    old_receipt["mutation_started"] = True
    activation._persist(paths, old, old_receipt, "rollback-failed")
    old_bytes = (old / "receipt.json").read_bytes()
    activation._atomic(paths.recovery_required, (str(old) + "\n").encode())
    marker_bytes = paths.recovery_required.read_bytes()
    tx, receipt = activation.new_transaction(paths, source)
    package_dir = tx / "artifacts/package"
    package_dir.mkdir(parents=True)
    wheel_b = Path(shutil.copy2(wheel_b, package_dir / wheel_b.name))
    manifest_b = Path(shutil.copy2(manifest_b, package_dir / manifest_b.name))
    receipt["package"] = {"wheel": str(wheel_b), "manifest": str(manifest_b),
        "release_id": release_b["release_id"], "wheel_sha256": activation._sha(wheel_b)}
    for unit in (*activation.SERVICES, activation.WATCHDOG_UNIT):
        activation._atomic(tx / "staged-units" / unit, b"fixture unit", 0o644)
    receipt["artifacts"] = {"files": {str(p.relative_to(tx)): activation._fingerprint(p)
        for base in (tx / "artifacts", tx / "staged-units") for p in base.rglob("*") if p.is_file()}}
    activation._persist(paths, tx, receipt, "artifacts-staged")
    _inject_post_install_failure(monkeypatch)
    # Service/TLS contracts are covered separately by the real signer fixture
    # and live read-only qualification. This layer executes actual A/B wheels,
    # wrapper publication, frozen recovery capsule, journals and own inverse.
    monkeypatch.setattr(recovery, "_baseline", lambda p: {
        "state": activation._managed_release_state_snapshot(p, require_generation=True),
        "tls": {"consumer_tls": {"fixture": "unchanged"}}})
    recovery.stabilize(paths, tx, old, activation._sha(old / "receipt.json"))
    if failure:
        with pytest.raises(RuntimeError, match="post-install activation failure"):
            activation.execute(paths, tx)
        assert activation.load_transaction(paths, tx)[1]["status"] == "rolled-back"
        assert paths.recovery_required.read_bytes() == marker_bytes
    else:
        monkeypatch.setattr(activation, "_publish_units", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(activation, "_capture_generated_runtime_postimages", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(activation, "_health", lambda: {"backend": "ok"})
        result = activation.execute(paths, tx)
        assert result["action"] == "activated"
        assert activation._managed_release_id(paths) == release_b["release_id"]
        assert not paths.recovery_required.exists()
        activation.rollback(paths, tx, reason="qualify fresh transaction own inverse")
        assert activation.load_transaction(paths, tx)[1]["status"] == "rolled-back"
    assert activation._managed_release_id(paths) == release_a["release_id"]
    assert {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in before} == before
    assert (old / "receipt.json").read_bytes() == old_bytes
    assert release_manager.verify_installed_tree(paths.managed_root / "releases" / release_b["release_id"])["ok"]
