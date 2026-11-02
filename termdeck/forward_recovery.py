"""Explicit forward recovery from a preserved, irrecoverable activation.

An old failed inverse stays failed. A fresh transaction may adopt a verified
current baseline, and discharge the old gate only after its own activation is
durably successful. All entrypoints run under web_activation's activation lock.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from . import release_manager, runtime_attestation, service_wrappers

SCHEMA = "termdeck.activation-forward-recovery.v1"
MAX_ADMISSION_AGE = 600


def _api():
    from . import web_activation
    return web_activation


def _image(path):
    api = _api()
    path, capture = api._read_regular(path, label="forward recovery input")
    return {**api._fingerprint_from_capture(path, capture),
            "identity": api._file_identity_payload(capture.identity)}, capture


def _failed(paths, old_tx, expected_digest):
    api = _api()
    old_tx = api._publication_path(old_tx, label="failed activation transaction")
    if old_tx.parent != paths.evidence or not old_tx.name.startswith("tx-"):
        raise api.WebActivationError("failed transaction must be a direct canonical evidence child")
    image, capture = _image(old_tx / "receipt.json")
    if not api._lower_hex(expected_digest, length=64) or image["sha256"] != expected_digest:
        raise api.WebActivationError("failed activation receipt differs from the reviewed digest")
    old = api._strict_json_object(capture.snapshot.content, label="failed activation receipt")
    expected = {"schema": api.SCHEMA, "tx_id": old_tx.name,
                **{key: str(getattr(paths, key)) for key in
                   ("runtime", "database", "managed_root", "managed_bin", "unit_dir")},
                "services": list(api.SERVICES), "status": "rollback-failed", "mutation_started": True}
    if any(old.get(key) != value for key, value in expected.items()):
        raise api.WebActivationError("failed activation has a different recovery boundary or state")
    return {"transaction": str(old_tx), "receipt": image}


def _marker(paths, old_tx):
    api = _api()
    image, capture = _image(paths.recovery_required)
    if capture.snapshot.content != (str(old_tx) + "\n").encode():
        raise api.WebActivationError("recovery marker does not name the reviewed failed transaction")
    return image


def _candidate(paths, tx, receipt):
    api = _api()
    expected_boundary = {"schema": api.SCHEMA, "tx_id": tx.name,
        **{key: str(getattr(paths, key)) for key in
           ("target", "runtime", "database", "managed_root", "managed_bin", "unit_dir")},
        "services": list(api.SERVICES)}
    if any(receipt.get(key) != value for key, value in expected_boundary.items()):
        raise api.WebActivationError("forward recovery candidate has a different activation boundary")
    package = receipt.get("package")
    artifacts = receipt.get("artifacts")
    if not isinstance(package, dict) or not isinstance(artifacts, dict):
        raise api.WebActivationError("forward recovery requires a qualified candidate")
    files = artifacts.get("files")
    required = {"staged-runtime/" + name for name in api._sources(paths, tx)}
    required.update("staged-units/" + name for name in (*api.SERVICES, api.WATCHDOG_UNIT))
    if not isinstance(files, dict) or not required.issubset(files):
        raise api.WebActivationError("forward recovery candidate inventory is incomplete")
    images = {}
    for relative, expected in files.items():
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise api.WebActivationError("forward recovery artifact escapes its transaction")
        image, _capture = _image(tx / relative)
        if not isinstance(expected, dict) or any(image.get(k) != v for k, v in expected.items()):
            raise api.WebActivationError("forward recovery candidate artifact changed")
        images[relative] = image
    for name in ("wheel", "manifest"):
        path = api._publication_path(package.get(name, ""), label="forward recovery package")
        if path.parent != tx / "artifacts" / "package" or str(path.relative_to(tx)) not in images:
            raise api.WebActivationError("forward recovery package is outside its qualified inventory")
    verified = release_manager.verify_wheel(package["wheel"], manifest_path=package["manifest"])
    if (verified.manifest is None or verified.manifest["release_id"] != package.get("release_id")
            or api._sha(Path(package["wheel"])) != package.get("wheel_sha256")):
        raise api.WebActivationError("forward recovery package identity changed")
    source = api._source(paths)
    if any(source.get(k) != receipt.get("source", {}).get(k) for k in ("commit", "tree")):
        raise api.WebActivationError("forward recovery source changed")
    return {"transaction": str(tx), "target": str(paths.target), "source": receipt["source"],
            "package": package, "artifacts": images}


def _tls(paths):
    """Verify signed producer bytes against separately observed consumer inodes.

    This does not contact the broker: its verify operation seeds private inputs.
    No TLS bytes or private keys are retained in the recovery record.
    """
    api = _api()
    image, capture = _image(paths.runtime / "build-receipt.json")
    signed = api._strict_json_object(capture.snapshot.content, label="forward recovery runtime receipt")
    try:
        runtime_attestation.verify_runtime_build_attestation(signed, config=api._runtime_attestation_config(paths))
    except runtime_attestation.AttestationError as exc:
        raise api.WebActivationError("forward recovery runtime signature is invalid") from exc
    tls = api._capture_restored_consumer_tls(
        paths, signed["tls_publication"]["generation"], signed["tls_publication"]["current"],
    )
    images = {}
    for relative, claim in signed["runtime_publication"]["outputs"].items():
        observed, _capture = _image(paths.runtime / relative)
        if observed["sha256"] != claim["sha256"] or observed["mode"] != claim["mode"]:
            raise api.WebActivationError("forward recovery runtime output differs from signed producer")
        images[relative] = observed
    for relative, digest in signed["files"].items():
        observed, _capture = _image(paths.runtime / relative)
        if observed["sha256"] != digest:
            raise api.WebActivationError("forward recovery runtime source differs from signed producer")
        images[relative] = observed
    if api._observed_primary_lan_ipv4(paths) != signed["lan_ip"]:
        raise api.WebActivationError("forward recovery LAN address changed")
    api._run(["/usr/bin/openssl", "x509", "-in", str(paths.runtime / "tls/server.crt"),
              "-noout", "-checkend", "2592000"], timeout=10,
             env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    api._run(["/usr/bin/openssl", "verify", "-CAfile", str(paths.runtime / "tls/ca.crt"),
              "-verify_ip", signed["lan_ip"], str(paths.runtime / "tls/server.crt")], timeout=10,
             env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    if _image(paths.runtime / "build-receipt.json")[0] != image:
        raise api.WebActivationError("forward recovery runtime receipt changed while verifying")
    api._runtime_tls_publication(paths, tls)
    for relative, expected in images.items():
        if _image(paths.runtime / relative)[0] != expected:
            raise api.WebActivationError("forward recovery runtime files changed while verifying")
    return {"receipt": image, "consumer_tls": tls, "files": images,
            "publisher_sha256": api._installed_tls_reuse_publisher_sha256(paths)}


def _baseline(paths):
    api = _api()
    layout = release_manager.release_layout(root=paths.managed_root, bin_dir=paths.managed_bin)
    with release_manager.release_lock(layout):
        release_manager._verify_managed_activation(layout, release_manager.load_managed_state(layout), verify_release=True)
        state = api._managed_release_state_snapshot(paths, require_generation=True)
        if state is None or not state["release_id"]:
            raise api.WebActivationError("forward recovery requires a verified managed release")
    # The public wrapper binding API acquires the release lock itself. Do not
    # nest its independent flock inside the descriptor above.
    wrappers = api._preflight_watchdog_wrapper(paths, state["release_id"])
    wrapper_layout = service_wrappers.resolve_layout(**api._wrapper_kwargs(paths))
    services = {}
    observed = {}
    for unit in api.SERVICES:
        result = api._systemd("show", unit, "--no-pager", *[v for key in (
            "MainPID", "InvocationID", "ActiveState", "SubState", "LoadState", "Type",
            "FragmentPath", "DropInPaths", "NeedDaemonReload") for v in ("-p", key)])
        values = dict(line.partition("=")[::2] for line in result.stdout.splitlines() if "=" in line)
        spec = service_wrappers.UNIT_SPEC_BY_NAME[unit]
        command = service_wrappers.build_start_command(spec, **api._wrapper_kwargs(paths))
        if not api._web_service_start_ready(paths, unit, values, layout=wrapper_layout,
                command=command, managed=True, release_id=state["release_id"], observed=observed):
            raise api.WebActivationError("forward recovery service is not ready")
        services[unit] = {"properties": values, "identity": observed[unit],
                          "unit": _image(paths.unit_dir / unit)[0]}
    tls = _tls(paths)
    health = api._health()
    if api._managed_release_state_snapshot(paths, require_generation=True) != state:
        raise api.WebActivationError("forward recovery managed release changed")
    for unit, expected in services.items():
        pid, ticks, _invocation = observed[unit]
        current = service_wrappers.process_identity(pid)
        if current["start_ticks"] != ticks or _image(paths.unit_dir / unit)[0] != expected["unit"]:
            raise api.WebActivationError("forward recovery service changed while verifying")
    # Normalize tuple/path values before a durable receipt round-trip.
    import json
    return json.loads(json.dumps({"state": state, "wrappers": wrappers,
                                 "services": services, "tls": tls, "health": health}, default=str))


def stabilize(paths, tx, old_tx, expected_digest):
    api = _api()
    tx = api._publication_path(tx, label="forward recovery transaction")
    if tx.resolve() != tx:
        raise api.WebActivationError("forward recovery transaction must not use a symlink alias")
    tx, receipt = api.load_transaction(paths, tx)
    if receipt.get("status") == "activated":
        if os.environ.get("TERMDECK_ACTIVATION_APPROVED") != "1":
            raise api.WebActivationError("gate discharge requires TERMDECK_ACTIVATION_APPROVED=1 after review")
        binding = _binding(receipt.get("forward_recovery"))
        if (binding["failed"]["transaction"] != str(old_tx)
                or binding["failed"]["receipt"]["sha256"] != expected_digest):
            raise api.WebActivationError("completion does not match the reviewed forward recovery")
        complete(paths, tx, receipt)
        return {"ok": True, "action": "forward-recovery-completed", "transaction": str(tx)}
    if receipt.get("status") != "artifacts-staged" or receipt.get("mutation_started"):
        raise api.WebActivationError("stabilize requires a fresh artifacts-staged transaction")
    if paths.evidence != paths.database.parent / "recovery" / "activation-v2" or tx == old_tx:
        raise api.WebActivationError("forward recovery requires distinct canonical transactions")
    failed = _failed(paths, old_tx, expected_digest)
    marker = _marker(paths, old_tx)
    candidate = _candidate(paths, tx, receipt)
    baseline = _baseline(paths)
    if _failed(paths, old_tx, expected_digest) != failed or _marker(paths, old_tx) != marker:
        raise api.WebActivationError("failed recovery evidence changed during stabilization")
    receipt["forward_recovery"] = {
        "schema": SCHEMA, "failed": failed, "marker": marker, "candidate": candidate,
        "baseline": baseline, "prepared_epoch": time.time(),
        "historical_inverse": "failed; not replayed or certified",
    }
    api._persist(paths, tx, receipt)
    return {"ok": True, "action": "forward-recovery-prepared", "transaction": str(tx),
            "review": receipt["forward_recovery"]}


def admit(paths, tx, receipt):
    """Fail before any mutation unless the live old gate is explicitly bound."""
    api = _api()
    binding = receipt.get("forward_recovery")
    if binding is None:
        if os.path.lexists(paths.recovery_required):
            raise api.WebActivationError("unresolved recovery marker requires reviewed --stabilize admission")
        return
    binding = _binding(binding)
    epoch = binding.get("prepared_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, (float, int)) or not 0 <= time.time() - epoch <= MAX_ADMISSION_AGE:
        raise api.WebActivationError("forward recovery admission expired; stabilize and review again")
    old_tx = Path(binding["failed"]["transaction"])
    if (_failed(paths, old_tx, binding["failed"]["receipt"]["sha256"]) != binding["failed"]
            or _marker(paths, old_tx) != binding["marker"]
            or _candidate(paths, tx, receipt) != binding["candidate"]
            or _baseline(paths) != binding["baseline"]):
        raise api.WebActivationError("forward recovery admission changed; stabilize and review again")


def complete(paths, tx, receipt):
    """Journal success before CAS deletion; retry after a crash is supported."""
    api = _api()
    binding = receipt.get("forward_recovery")
    if binding is None:
        return
    binding = _binding(binding)
    if receipt.get("status") != "activated" or binding.get("schema") != SCHEMA:
        raise api.WebActivationError("only durable activation may discharge a failed recovery gate")
    old_tx = Path(binding["failed"]["transaction"])
    if _failed(paths, old_tx, binding["failed"]["receipt"]["sha256"]) != binding["failed"]:
        raise api.WebActivationError("historical failed recovery evidence changed")
    current = _baseline(paths)
    if (current["state"]["release_id"] != receipt["package"]["release_id"]
            or current["tls"]["consumer_tls"] != binding["baseline"]["tls"]["consumer_tls"]):
        raise api.WebActivationError("completed forward recovery has drifted")
    if not os.path.lexists(paths.recovery_required):
        if binding.get("completion") not in ("discharge-prepared", "completed"):
            raise api.WebActivationError("forward recovery marker disappeared before discharge journal")
    else:
        if _marker(paths, old_tx) != binding["marker"]:
            raise api.WebActivationError("forward recovery marker changed before discharge")
        guards = api._capture_parent_guards((paths.recovery_required,))
        _path, capture = api._capture_anchored(paths.recovery_required, label="forward recovery marker", parent_guards=guards)
        if api._file_identity_payload(capture.identity) != binding["marker"]["identity"]:
            raise api.WebActivationError("forward recovery marker replaced before discharge")
        binding["completion"] = "discharge-prepared"
        api._persist(paths, tx, receipt)
        api._publish_anchored(paths.recovery_required, None, capture.snapshot.mode,
            label="successful forward recovery gate", parent_guards=guards, expected_preimage=capture)
    binding["completion"] = "completed"
    api._persist(paths, tx, receipt)


def _binding(value):
    api = _api()
    required = {"schema", "failed", "marker", "candidate", "baseline", "prepared_epoch", "historical_inverse"}
    if (not isinstance(value, dict) or not required.issubset(value)
            or set(value) - required - {"completion"} or value.get("schema") != SCHEMA
            or any(not isinstance(value[key], dict) for key in ("failed", "marker", "candidate", "baseline"))
            or not isinstance(value["failed"].get("transaction"), str)
            or not isinstance(value["failed"].get("receipt"), dict)
            or not _api()._lower_hex(value["failed"]["receipt"].get("sha256"), length=64)
            or not isinstance(value["baseline"].get("tls"), dict)
            or "consumer_tls" not in value["baseline"]["tls"]
            or value.get("completion") not in (None, "discharge-prepared", "completed")):
        raise api.WebActivationError("forward recovery admission schema is invalid")
    return value
