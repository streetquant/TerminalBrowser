from __future__ import annotations

import base64
from contextlib import contextmanager
import csv
import dataclasses
import hashlib
import io
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from termdeck import release_manager
from termdeck import runtime_attestation
from termdeck import web_activation as activation
from termdeck.build_info import (
    BUILD_INFO_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    canonical_json_bytes,
    compute_build_id,
    compute_release_id,
    sha256_file,
)


def _paths(tmp_path: Path) -> activation.ActivationPaths:
    return activation.ActivationPaths(
        target=tmp_path / "source",
        evidence=tmp_path / "recovery" / "activation-v2",
        runtime=tmp_path / "runtime" / "termdeck-lan",
        database=tmp_path / "sessions.sqlite3",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )


def _test_attestation_config(
    paths: activation.ActivationPaths,
) -> runtime_attestation.DaemonConfig:
    """Explicit disposable trust boundary; production NSS is never replaced."""

    control = paths.runtime.parent / "test-attestor"
    return runtime_attestation.DaemonConfig(
        control_root=control,
        socket_path=control / "runtime-attestd.sock",
        records_path=control / "records",
        private_key_path=control / "runtime-attestd.key",
        public_key_path=paths.runtime.parent / "test-runtime-attestd.pub",
        runtime_receipt_path=paths.runtime / "build-receipt.json",
        preparer=runtime_attestation.Principal(
            "test-preparer", os.geteuid(), os.getegid()
        ),
        root_uid=os.geteuid(),
        root_gid=os.getegid(),
    )


@pytest.fixture(autouse=True)
def _use_disposable_attestation_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only account/path configuration is isolated.  The producer and consumer
    # execute their real Ed25519 signing, descriptor, and signature checks.
    monkeypatch.setattr(
        activation, "_runtime_attestation_config", _test_attestation_config
    )


def _prepare_test_attestor(
    paths: activation.ActivationPaths,
) -> runtime_attestation.DaemonConfig:
    config = _test_attestation_config(paths)
    config.control_root.mkdir(parents=True, exist_ok=True)
    config.control_root.chmod(runtime_attestation.CONTROL_DIRECTORY_MODE)
    config.records_path.mkdir(exist_ok=True)
    config.records_path.chmod(runtime_attestation.RECORDS_DIRECTORY_MODE)
    if not config.public_key_path.exists():
        runtime_attestation.generate_keypair(config)
    return config


def _release_record_hash(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(
        b"="
    ).decode("ascii")


def _release_artifact(tmp_path: Path, seed: str, *, module_overrides: dict[str, bytes] | None = None) -> tuple[Path, Path, dict[str, object]]:
    """Build a self-contained, authenticated wheel for activation rollback tests."""

    # Current managed releases must bind wrapper execution to their frozen
    # wheel site.  Keep the integration artifact on the release-manager
    # enforcement side of that compatibility boundary.
    version = "0.2.21"
    project = Path(__file__).parents[1]
    package = project / "termdeck"
    build_info: dict[str, object] = {
        "schema": BUILD_INFO_SCHEMA,
        "version": version,
        "source_commit": seed * 40,
        "source_tree_sha256": seed * 64,
        "source_date_epoch": 1_700_000_000,
        "build_mode": "release",
    }
    build_info["build_id"] = compute_build_id(build_info)
    members = {
        str(path.relative_to(project)): path.read_bytes()
        for path in sorted(package.rglob("*.py"))
    }
    members.update(module_overrides or {})
    # The staged-import and historical-activation checks both import from the
    # wheel.  Keep their small public surface version-consistent while carrying
    # the real release-manager dependency graph for the authenticated rollback.
    members["termdeck/__init__.py"] = f'__version__ = "{version}"\n'.encode("utf-8")
    members["termdeck/cli.py"] = b"def main():\n    return 0\n"
    members["termdeck/_build_info.json"] = canonical_json_bytes(build_info)
    dist_info = f"t1000-{version}.dist-info"
    members[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: t1000\nVersion: {version}\n\n"
    ).encode("utf-8")
    members[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode("utf-8")
    members[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\nt1000 = termdeck.cli:main\n"
    record_name = f"{dist_info}/RECORD"
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(
        [
            (name, _release_record_hash(content), str(len(content)))
            for name, content in sorted(members.items())
        ]
        + [(record_name, "", "")]
    )
    members[record_name] = record.getvalue().encode("utf-8")

    artifact_root = tmp_path / "artifacts" / seed
    wheel = artifact_root / f"t1000-{version}-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    wheel_hash = sha256_file(wheel)
    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "release_id": compute_release_id(str(build_info["build_id"]), wheel_hash),
        "build_info": build_info,
        "wheel": wheel.name,
        "wheel_sha256": wheel_hash,
        "wheel_size": wheel.stat().st_size,
    }
    manifest_path = artifact_root / f"{wheel.stem}.release.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return wheel, manifest_path, manifest


def _integration_paths(tmp_path: Path) -> activation.ActivationPaths:
    # Match release_manager's disposable layout exactly so outer activation
    # preimages cover the wrappers that the real package transaction changes.
    return activation.ActivationPaths(
        target=Path(__file__).parents[1].resolve(),
        evidence=tmp_path / "state" / "recovery" / "activation-v2",
        runtime=tmp_path / "termdeck-lan",
        database=tmp_path / "state" / "sessions.sqlite3",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )


def _storage_attestation() -> SimpleNamespace:
    return SimpleNamespace(
        as_dict=lambda: {
            "schema": "termdeck.storage-fabric-attestation.v1",
            "fabric": "warp-drives-v1",
            "storage_class": "ssd-cell",
            "production": True,
            "path_evidence": [],
        }
    )


def _fixture_wrapper_execution_identity(release_id: str) -> dict[str, object]:
    """Minimal immutable binding for fixtures that replace release-manager IO."""

    return {
        "execution_root": f"/fixture/releases/{release_id}/site",
        "execution_binding": {
            "termdeck/web_activation.py": {"sha256": "a" * 64, "size": 1},
        },
        "control_root": "/fixture/control",
        "control_socket": "/fixture/control/runtime-attestd.sock",
        "control_attestation": {
            "socket": "/fixture/control/runtime-attestd.sock",
            "operation": "sign",
            "request_schema": "termdeck.lan-runtime-build.v2",
            "response_schema": "termdeck.runtime-attestation.v1",
            "producer_principal": "termdeck-preparer",
            "excluded_principal": "max",
            "control_root_owner": "root",
            "control_root_group": "termdeck-preparer",
            "control_root_mode": 0o710,
            "socket_owner": "root",
            "socket_group": "termdeck-preparer",
            "socket_mode": 0o660,
        },
    }


def _file_preimage(path: Path) -> tuple[bytes, int] | None:
    """Return a small exact file fixture without following a symlink."""

    if not os.path.lexists(path):
        return None
    mode = path.lstat().st_mode
    assert stat.S_ISREG(mode)
    return path.read_bytes(), stat.S_IMODE(mode)


def _write_runtime_preparer_outputs(
    paths: activation.ActivationPaths,
    *,
    generation: str = "a" * 32,
) -> None:
    """Write a faithful v2 LAN-preparer publication for activation tests.

    The activation layer must never adopt arbitrary files merely because they
    are regular and look plausible.  Fixtures that model the synchronous
    preparer therefore write its two output leaves first, bind their exact
    descriptor-captured identities in the v2 receipt, and publish that receipt
    last just as the real preparer does.
    """

    lan_ip = "192.168.1.42"
    # These are the exact source leaves which the real preparer commits into
    # its signed ``files`` digest map.  Keeping
    # them as actual anchored leaves lets the activation validator reject a
    # receipt copied from another runtime or a later source/port mutation.
    source_payloads = {
        "bin/termdeck-web-server": b"#!/bin/sh\nexec true\n",
        "nginx/nginx.conf": b"server {\n    listen 0.0.0.0:7443 ssl;\n}\n",
        "tls/ca.crt": b"test-ca\n",
        "tls/server-chain.crt": b"test-chain\n",
        "tls/server.crt": b"test-server\n",
    }
    tls = paths.runtime / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    tls.chmod(0o700)
    for relative, content in source_payloads.items():
        name = Path(relative).name
        activation._atomic(
            paths.runtime / relative,
            content,
            activation.RUNTIME_TLS_REQUIRED_MODES.get(name, 0o600),
        )
    # The v2 receipt binds the full served TLS generation, including leaves
    # which are not source inputs.  They need not be valid certificates for
    # this activation-layer fixture; their descriptor identity and digest are
    # what the preparer commits and the consumer verifies.
    for name, content, mode in (
        ("ca.key", b"test-ca-key\n", 0o600),
        ("server.key", b"test-server-key\n", 0o600),
        ("server.ext", b"test-server-ext\n", 0o600),
    ):
        activation._atomic(tls / name, content, mode)

    config = _prepare_test_attestor(paths)

    payloads = {
        "current-lan-ip": (lan_ip + "\n").encode("ascii"),
        "nginx/allowed-hosts.map": activation._runtime_allowed_hosts(lan_ip),
    }
    outputs: dict[str, dict[str, object]] = {}
    for relative, content in payloads.items():
        target = paths.runtime / relative
        activation._atomic(target, content, 0o600)
        guards = activation._capture_parent_guards((target,))
        _path, capture = activation._capture_anchored(
            target,
            label=f"test runtime preparer output {relative}",
            parent_guards=guards,
        )
        assert capture.identity is not None
        outputs[relative] = {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": 0o600,
            "identity": [
                capture.identity.device,
                capture.identity.inode,
                capture.identity.mode,
                capture.identity.nlink,
                capture.identity.size,
                capture.identity.mtime_ns,
                capture.identity.ctime_ns,
            ],
        }
    files: dict[str, str] = {}
    for relative in sorted(activation.RUNTIME_PREPARER_SOURCE_FILES):
        target = paths.runtime / relative
        guards = activation._capture_parent_guards((target,))
        _path, capture = activation._capture_anchored(
            target,
            label=f"test runtime preparer source {relative}",
            parent_guards=guards,
        )
        assert capture.identity is not None
        digest = hashlib.sha256(capture.snapshot.content).hexdigest()
        files[relative] = digest

    tls_manifest: dict[str, dict[str, object]] = {}
    for name in sorted(activation.RUNTIME_TLS_REQUIRED_LEAVES):
        target = tls / name
        guards = activation._capture_parent_guards((target,))
        _path, capture = activation._capture_anchored(
            target,
            label=f"test runtime preparer TLS leaf {name}",
            parent_guards=guards,
        )
        assert capture.identity is not None
        tls_manifest[name] = {
            "identity": [
                capture.identity.device,
                capture.identity.inode,
                capture.identity.mode,
                capture.identity.nlink,
                capture.identity.size,
                capture.identity.mtime_ns,
                capture.identity.ctime_ns,
            ],
            "mode": capture.snapshot.mode,
            "sha256": hashlib.sha256(capture.snapshot.content).hexdigest(),
        }
    tls_info = tls.stat()
    tls_publication = {
        "generation": generation,
        "current": {
            "path": "tls",
            "identity": [
                tls_info.st_dev,
                tls_info.st_ino,
                stat.S_IMODE(tls_info.st_mode),
                tls_info.st_uid,
            ],
            "manifest": tls_manifest,
        },
        "prior": None,
    }

    receipt_path = paths.runtime / "build-receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish the unsigned core on the exact inode passed to the attestor,
    # then append its genuine detached signature while retaining that inode.
    core = {
        "files": files,
        "https_port": 7443,
        "lan_ip": lan_ip,
        "ok": True,
        "runtime_publication": {
            "generation": generation,
            "outputs": outputs,
        },
        "tls_publication": tls_publication,
        "schema": activation.RUNTIME_PUBLICATION_SCHEMA,
    }
    descriptor = os.open(
        receipt_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        os.fchmod(descriptor, 0o644)
        os.write(descriptor, runtime_attestation._canonical_json(core))
        os.fsync(descriptor)
        request = runtime_attestation.build_request(
            transaction_id=generation,
            receipt_path=receipt_path,
            receipt_descriptor=descriptor,
            config=config,
        )
        response = runtime_attestation.RuntimeAttestationDaemon(config).attest(
            request,
            receipt_descriptor=descriptor,
            peer=runtime_attestation.PeerCredentials(
                os.getpid(), os.geteuid(), os.getegid()
            ),
        )
        receipt = {**core, "producer_attestation": response}
        payload = runtime_attestation._canonical_json(receipt)
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rewrite_regular_same_inode(path: Path, payload: bytes) -> None:
    """Rewrite a fixture leaf without replacing the receipt-bound inode."""

    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            assert written > 0
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _generated_runtime_preimages(
    paths: activation.ActivationPaths,
    tx: Path,
) -> dict[str, dict[str, object]]:
    return {
        "generated:" + relative: activation._preimage(
            paths.runtime / relative,
            tx,
            "generated-" + relative.replace("/", "-"),
        )
        for relative in activation.GENERATED_RUNTIME
    }


def _inject_post_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda *_args: {})
    # The integration assertions below exercise the real release-manager B
    # rollback.  Keep their injected failure after package/wrapper publication
    # rather than making the A/B watchdog boundary itself fail first.  The
    # unit publication and state/wrapper binding still use the real activation
    # path; only the wrapper module's readback is isolated from unrelated
    # runtime-process fixtures.
    def preflight_wrapper(paths: activation.ActivationPaths, release_id: str) -> dict[str, object]:
        manifest = json.loads((paths.managed_root / "wrapper-manifest.json").read_text())
        assert manifest["release_id"] == release_id
        return {
            "ok": True,
            "release_id": release_id,
            "manifest_sha256": manifest["manifest_sha256"],
            **activation._wrapper_execution_identity(paths, release_id),
        }

    monkeypatch.setattr(activation, "_preflight_watchdog_wrapper", preflight_wrapper)
    monkeypatch.setattr(
        activation,
        "_publish_units",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("post-install activation failure")),
    )
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(activation, "_stop_web", lambda: None)
    monkeypatch.setattr(activation, "_disarm", lambda: None)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda *_args: {"integrity": "ok", "lost_session_ids": [], "preserved_session_ids": 0},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda *_args: {"disappeared": [], "natural_exit_allowed": True},
    )


def _hard_crash_after_release_callback(
    paths: activation.ActivationPaths,
    tx: Path,
) -> subprocess.CompletedProcess[str]:
    """Execute activation in a child and exit after its durable B callback."""

    child = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from termdeck import release_manager
from termdeck import web_activation as activation
target, evidence, runtime, database, managed_root, managed_bin, unit_dir, transaction = map(Path, sys.argv[1:9])
paths = activation.ActivationPaths(target, evidence, runtime, database, managed_root, managed_bin, unit_dir)
class Attestation:
    def as_dict(self):
        return {"schema":"termdeck.storage-fabric-attestation.v1","fabric":"warp-drives-v1","storage_class":"ssd-cell","production":True,"path_evidence":[]}
release_manager._require_managed_storage = lambda _layout: Attestation()
activation.snapshot_database = lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"}
activation.process_snapshot = lambda: []
activation._sources = lambda *_args: {}
activation._systemd = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
activation._start_web = lambda _paths: None
activation._stop_web = activation._disarm = lambda: None
activation.verify_database = lambda *_args: {"integrity":"ok","lost_session_ids":[],"preserved_session_ids":0}
activation.process_observation = lambda *_args: {"disappeared":[],"natural_exit_allowed":True}
def preflight(value, expected):
    manifest = json.loads((value.managed_root / "wrapper-manifest.json").read_text())
    assert manifest["release_id"] == expected
    return {
        "ok": True,
        "release_id": expected,
        "manifest_sha256": manifest["manifest_sha256"],
        **activation._wrapper_execution_identity(value, expected),
    }
activation._preflight_watchdog_wrapper = preflight
original = activation._checkpoint_publication
def crash(*args):
    original(*args)
    if args[-1] == "install:publication-postimage": os._exit(86)
activation._checkpoint_publication = crash
os.environ["TERMDECK_ACTIVATION_APPROVED"] = "1"
activation.execute(paths, transaction)
'''
    return subprocess.run(
        [
            sys.executable, "-c", child,
            str(paths.target), str(paths.evidence), str(paths.runtime),
            str(paths.database), str(paths.managed_root), str(paths.managed_bin),
            str(paths.unit_dir), str(tx),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "TERMDECK_ACTIVATION_APPROVED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )


def _hard_crash_after_release_commit(
    paths: activation.ActivationPaths,
    tx: Path,
) -> subprocess.CompletedProcess[str]:
    """Die after RM committed B but before outer activation proceeds."""

    child = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from termdeck import release_manager
from termdeck import web_activation as activation
target, evidence, runtime, database, managed_root, managed_bin, unit_dir, transaction = map(Path, sys.argv[1:9])
paths = activation.ActivationPaths(target, evidence, runtime, database, managed_root, managed_bin, unit_dir)
class Attestation:
    def as_dict(self):
        return {"schema":"termdeck.storage-fabric-attestation.v1","fabric":"warp-drives-v1","storage_class":"ssd-cell","production":True,"path_evidence":[]}
release_manager._require_managed_storage = lambda _layout: Attestation()
activation.snapshot_database = lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"}
activation.process_snapshot = lambda: []
activation._sources = lambda *_args: {}
activation._systemd = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
activation._start_web = lambda _paths: None
activation._stop_web = activation._disarm = lambda: None
activation.verify_database = lambda *_args: {"integrity":"ok","lost_session_ids":[],"preserved_session_ids":0}
activation.process_observation = lambda *_args: {"disappeared":[],"natural_exit_allowed":True}
def preflight(value, expected):
    manifest = json.loads((value.managed_root / "wrapper-manifest.json").read_text())
    assert manifest["release_id"] == expected
    return {
        "ok": True,
        "release_id": expected,
        "manifest_sha256": manifest["manifest_sha256"],
        **activation._wrapper_execution_identity(value, expected),
    }
activation._preflight_watchdog_wrapper = preflight
original = activation._checkpoint_publication
def crash(*args):
    original(*args)
    if args[-1] == "package:managed-release": os._exit(88)
activation._checkpoint_publication = crash
os.environ["TERMDECK_ACTIVATION_APPROVED"] = "1"
activation.execute(paths, transaction)
'''
    return subprocess.run(
        [
            sys.executable, "-c", child,
            str(paths.target), str(paths.evidence), str(paths.runtime),
            str(paths.database), str(paths.managed_root), str(paths.managed_bin),
            str(paths.unit_dir), str(tx),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "TERMDECK_ACTIVATION_APPROVED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )


def _hard_crash_after_release_inverse_callback(
    paths: activation.ActivationPaths,
    tx: Path,
) -> subprocess.CompletedProcess[str]:
    """Start a fresh inverse and die after its durable A callback hand-off."""

    child = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from termdeck import release_manager
from termdeck import web_activation as activation
target, evidence, runtime, database, managed_root, managed_bin, unit_dir, transaction = map(Path, sys.argv[1:9])
paths = activation.ActivationPaths(target, evidence, runtime, database, managed_root, managed_bin, unit_dir)
class Attestation:
    def as_dict(self):
        return {"schema":"termdeck.storage-fabric-attestation.v1","fabric":"warp-drives-v1","storage_class":"ssd-cell","production":True,"path_evidence":[]}
release_manager._require_managed_storage = lambda _layout: Attestation()
activation._systemd = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
activation._start_web = lambda _paths: None
activation._stop_web = activation._disarm = lambda: None
activation.verify_database = lambda *_args: {"integrity":"ok","lost_session_ids":[],"preserved_session_ids":0}
activation.process_observation = lambda *_args: {"disappeared":[],"natural_exit_allowed":True}
original = activation._checkpoint_publication
def crash(*args):
    original(*args)
    if args[-1] == "rollback:publication-postimage": os._exit(87)
activation._checkpoint_publication = crash
activation.rollback(paths, transaction, reason="hard death after inverse callback")
'''
    return subprocess.run(
        [
            sys.executable, "-c", child,
            str(paths.target), str(paths.evidence), str(paths.runtime),
            str(paths.database), str(paths.managed_root), str(paths.managed_bin),
            str(paths.unit_dir), str(tx),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "TERMDECK_ACTIVATION_APPROVED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )



def test_activation_defaults_bind_canonical_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "canonical" / "termdeck-state"
    monkeypatch.setenv("TERMDECK_STATE_ROOT", str(state_root))

    paths = activation.ActivationPaths.defaults(
        target=tmp_path / "source",
        evidence=tmp_path / "evidence",
        runtime=tmp_path / "runtime",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )

    assert paths.database == state_root / "sessions.sqlite3"


def test_activation_defaults_derive_recovery_evidence_under_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("TERMDECK_STATE_ROOT", str(state_root))

    paths = activation.ActivationPaths.defaults(
        target=tmp_path / "source",
        runtime=tmp_path / "runtime",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )

    assert paths.evidence == state_root / "recovery" / "activation-v2"


def test_activation_defaults_bind_warp_recovery_evidence_root(tmp_path: Path) -> None:
    paths = activation.ActivationPaths.defaults(
        target=tmp_path / "source",
        runtime=tmp_path / "runtime",
        database=tmp_path / "sessions.sqlite3",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )

    assert paths.evidence == Path(
        "/warp/projects/softwares/termdeck/.termdeck/state/recovery/activation-v2"
    )


def test_transaction_pointer_and_receipt_exist_before_build(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )

    assert receipt["status"] == "prepared"
    assert receipt["mutation_started"] is False
    pointer = activation._read(paths.latest)
    assert pointer["transaction"] == tx.name
    assert activation._read(tx / "receipt.json")["status"] == "prepared"

    activation._persist(paths, tx, receipt, status="building-package")
    assert activation._read(tx / "receipt.json")["mutation_started"] is False
    activation._persist(paths, tx, receipt, status="mutation-start")
    assert activation._read(tx / "receipt.json")["mutation_started"] is True


@pytest.mark.parametrize("status", sorted(activation.MUTATION_PHASES))
def test_every_mutation_phase_is_a_rollback_candidate(tmp_path: Path, status: str) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt["mutation_started"] = True
    receipt["status"] = status
    activation._write(tx / "receipt.json", receipt)

    loaded_tx, loaded = activation.load_transaction(paths, tx)
    assert loaded_tx == tx
    assert loaded["status"] == status


def test_live_backup_preserves_existing_session_ids_while_activity_changes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    connection = sqlite3.connect(paths.database)
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT)")
    connection.executemany("INSERT INTO sessions VALUES (?, ?)", [("keep-a", "running"), ("keep-b", "stopped")])
    connection.commit()
    connection.close()

    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    before = activation.snapshot_database(paths, tx, "before")
    connection = sqlite3.connect(paths.database)
    connection.execute("UPDATE sessions SET status='natural-exit' WHERE id='keep-a'")
    connection.execute("INSERT INTO sessions VALUES ('new-c', 'running')")
    connection.commit()
    connection.close()

    result = activation.verify_database(paths, tx, before)
    assert result["integrity"] == "ok"
    assert result["preserved_session_ids"] == 2
    assert result["new_session_ids"] == 1
    assert result["lost_session_ids"] == []
    assert result["wal_shm_byte_identity_required"] is False


def test_process_observation_records_natural_exit_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        activation,
        "process_snapshot",
        lambda: [{"pid": 20, "start_ticks": 4, "comm": "tmux"}],
    )
    result = activation.process_observation(
        [
            {"pid": 10, "start_ticks": 3, "comm": "codex"},
            {"pid": 20, "start_ticks": 4, "comm": "tmux"},
        ]
    )
    assert result["natural_exit_allowed"] is True
    assert result["protected_process_kill_evidence"]["allowlisted_units_only"] is True
    assert result["protected_process_kill_evidence"]["disappeared_processes_observed"] == result["disappeared"]
    assert result["disappeared"] == [{"comm": "codex", "pid": 10, "start_ticks": 3}]


def test_process_snapshot_observes_only_agent_independent_runtime_names(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(activation, "_run", fake_run)

    assert activation.process_snapshot() == []
    observed_names = [command[-1] for command in calls]
    assert observed_names == ["tmux", "t1000"]


def test_watchdog_is_armed_as_a_separate_user_unit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    unit = activation._watchdog_unit(paths, tmp_path / "evidence" / "tx-1")

    assert "termdeck-web-recovery.service" not in "\n".join(activation.SERVICES)
    assert f"ExecStart={paths.managed_root}/termdeck-web-recovery" in unit
    assert f"ExecStop={paths.managed_root}/termdeck-web-recovery-stop" in unit
    assert "--latest" not in unit
    assert "--transaction" not in unit
    assert str(tmp_path / "evidence" / "tx-1") not in unit
    assert "After=warp.mount default.target" in unit
    assert "ConditionPathIsMountPoint=/warp" in unit
    assert "RequiresMountsFor=" in unit
    assert "Restart=on-failure" in unit
    assert "RestartMode=direct" in unit
    assert "CODEX_BULK_STORAGE_MASKED" not in unit
    assert "warp-drives-v1" not in unit
    assert "ssd-cell" not in unit


def test_wrapper_publication_destinations_cover_all_managed_units(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    destinations = activation._wrapper_publication_paths(paths)
    expected_wrappers = {
        f"{stem}{suffix}"
        for stem in (
            "termdeck-web-backend",
            "termdeck-web-lan",
            "termdeck-web-recovery",
            "termdeck-cloudflared",
            "termdeck-cloudflare-preview",
            "termdeck-checkpoint",
        )
        for suffix in ("", "-stop")
    }

    assert len(destinations) == 19  # twelve wrappers, six configs, manifest
    assert len(expected_wrappers) == 12
    assert len(activation.service_wrappers.WRAPPER_NAMES) == 12
    assert set(activation.service_wrappers.WRAPPER_NAMES) == expected_wrappers
    assert {path.name for path in destinations[:12]} == expected_wrappers
    assert {path.name for path in destinations[12:18]} == set(activation.service_wrappers.UNIT_CONFIG_NAMES)
    assert destinations[-1] == paths.managed_root / activation.service_wrappers.WRAPPER_MANIFEST_NAME
    assert all(path.parent == paths.managed_root for path in destinations[:12])
    assert all(path.parent == paths.database.parent for path in destinations[12:18])


@pytest.mark.parametrize("canonical_runtime", (False, True))
def test_release_bound_wrapper_render_ignores_checkout_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_runtime: bool,
) -> None:
    """Recovery wrappers import the frozen release site, never checkout bytes."""

    paths = _paths(tmp_path)
    canonical_source = tmp_path / "canonical-source"
    if canonical_runtime:
        paths = dataclasses.replace(paths, runtime=tmp_path / "canonical-runtime")
        monkeypatch.setattr(activation.service_wrappers, "CANONICAL_RUNTIME_ROOT", paths.runtime)
        monkeypatch.setattr(activation.service_wrappers, "CANONICAL_SOURCE_ROOT", canonical_source)
    paths.target.mkdir()
    checkout_module = paths.target / "termdeck" / "web_activation.py"
    checkout_module.parent.mkdir()
    checkout_module.write_text("CHECKOUT_A = True\n", encoding="utf-8")
    release_id = "a" * 64
    execution_root = tmp_path / "managed" / "releases" / release_id / "site"
    execution_root.mkdir(parents=True)
    control_root = tmp_path / "control"
    binding = {"termdeck/web_activation.py": {"sha256": "b" * 64, "size": 1}}
    control_attestation = {
        "socket": str(control_root / activation.service_wrappers.CONTROL_ATTEST_SOCKET_NAME),
        "operation": activation.service_wrappers.CONTROL_ATTEST_OPERATION,
        "request_schema": activation.service_wrappers.CONTROL_ATTEST_REQUEST_SCHEMA,
        "response_schema": activation.service_wrappers.CONTROL_ATTEST_RESPONSE_SCHEMA,
        "producer_principal": activation.service_wrappers.CONTROL_ATTEST_PRODUCER,
        "excluded_principal": activation.service_wrappers.CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
        "control_root_owner": activation.service_wrappers.CONTROL_ATTEST_OWNER,
        "control_root_group": activation.service_wrappers.CONTROL_ATTEST_GROUP,
        "control_root_mode": activation.service_wrappers.CONTROL_ROOT_MODE,
        "socket_owner": activation.service_wrappers.CONTROL_ATTEST_OWNER,
        "socket_group": activation.service_wrappers.CONTROL_ATTEST_GROUP,
        "socket_mode": activation.service_wrappers.CONTROL_ATTEST_SOCKET_MODE,
    }
    monkeypatch.setattr(
        activation.release_manager,
        "release_wrapper_execution_binding",
        lambda **_kwargs: {
            "release_id": release_id,
            "immutable": True,
            "execution_root": execution_root,
            "execution_binding": binding,
            "control_root": str(control_root),
            "control_socket": control_attestation["socket"],
            "control_attestation": control_attestation,
        },
    )

    kwargs = activation._release_wrapper_kwargs(paths, release_id)
    before = activation.service_wrappers.render_wrappers(release_id, **kwargs)
    checkout_module.write_text("CHECKOUT_B = 'foreign policy'\n", encoding="utf-8")
    after = activation.service_wrappers.render_wrappers(release_id, **kwargs)
    recovery = before.wrappers[paths.managed_root / "termdeck-web-recovery"]

    assert before.wrappers == after.wrappers
    assert f"sys.path.insert(0, {json.dumps(str(execution_root))})".encode() in recovery
    assert b"CHECKOUT_B" not in recovery
    manifest = json.loads(before.manifest)
    assert manifest["source_root"] == str(canonical_source if canonical_runtime else paths.target)
    assert kwargs["runtime_root"] == (paths.runtime if canonical_runtime else paths.runtime.parent)
    assert manifest["execution_root"] == str(execution_root)
    assert manifest["execution_binding"] == binding


def test_custom_runtime_root_must_name_the_exact_lan_subtree(tmp_path: Path) -> None:
    """A custom activation cannot silently redirect preparation to /warp."""

    valid = _paths(tmp_path)
    assert activation._wrapper_runtime_root(valid) == tmp_path / "runtime"
    invalid = activation.ActivationPaths(
        target=valid.target,
        evidence=valid.evidence,
        runtime=tmp_path / "other-runtime",
        database=valid.database,
        managed_root=valid.managed_root,
        managed_bin=valid.managed_bin,
        unit_dir=valid.unit_dir,
    )
    with pytest.raises(activation.WebActivationError, match="exact <custom-root>/termdeck-lan"):
        activation._wrapper_runtime_root(invalid)


def test_static_units_match_managed_service_contract() -> None:
    roots = "/warp/projects/softwares/termdeck /warp/projects/softwares/termdeck/.termdeck /warp/projects/softwares/termdeck/.termdeck/state"
    units = {
        "termdeck-web-backend.service": {
            "stem": "termdeck-web-backend",
            "wants": ("network-online.target",),
            "after": ("warp.mount", "network-online.target"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": (
                    "/warp/projects/softwares/termdeck/.termdeck",
                    "/warp/projects/softwares/termdeck/.termdeck/state",
                ),
                "ConditionPathExists": (),
            },
        },
        "termdeck-web-lan.service": {
            "stem": "termdeck-web-lan",
            "wants": ("network-online.target", "termdeck-web-backend.service"),
            "after": ("warp.mount", "network-online.target", "termdeck-web-backend.service"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": ("/warp/projects/softwares/termdeck/.termdeck",),
                "ConditionPathExists": (),
            },
        },
        "termdeck-web-recovery.service": {
            "stem": "termdeck-web-recovery",
            "wants": (),
            "after": ("warp.mount", "default.target"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": (
                    "/warp/projects/softwares/termdeck",
                    "/warp/projects/softwares/termdeck/.termdeck/t1000-managed",
                    "/warp/projects/softwares/termdeck/.termdeck/bin",
                ),
                "ConditionPathExists": ("/warp/projects/softwares/termdeck/.termdeck/state/recovery/activation-v2/LATEST",),
            },
        },
        "termdeck-cloudflared.service": {
            "stem": "termdeck-cloudflared",
            "wants": ("network-online.target", "termdeck-web-lan.service"),
            "after": ("warp.mount", "network-online.target", "termdeck-web-lan.service"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": ("/warp/projects/softwares/termdeck/.termdeck",),
                "ConditionPathExists": (),
            },
        },
        "termdeck-cloudflare-preview.service": {
            "stem": "termdeck-cloudflare-preview",
            "wants": ("network-online.target", "termdeck-web-lan.service"),
            "after": ("warp.mount", "network-online.target", "termdeck-web-lan.service"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": ("/warp/projects/softwares/termdeck/.termdeck",),
                "ConditionPathExists": (),
            },
        },
        "termdeck-checkpoint.service": {
            "stem": "termdeck-checkpoint",
            "wants": (),
            "after": ("warp.mount", "user@1000.service", "local-fs.target"),
            "conditions": {
                "ConditionPathIsMountPoint": ("/warp",),
                "ConditionPathIsDirectory": (
                    "/warp/projects/softwares/termdeck/.termdeck/state",
                    "/warp/projects/softwares/termdeck/.termdeck/t1000-managed",
                ),
                "ConditionPathExists": (),
            },
        },
    }

    def directive_lines(content: str, directive: str) -> tuple[str, ...]:
        prefix = f"{directive}="
        return tuple(line[len(prefix) :] for line in content.splitlines() if line.startswith(prefix))

    def directive_values(content: str, directive: str) -> tuple[str, ...]:
        return tuple(value for line in directive_lines(content, directive) for value in line.split())

    expected_wrapper_names = {
        f"{contract['stem']}{suffix}" for contract in units.values() for suffix in ("", "-stop")
    }
    assert len(units) == 6
    assert len(expected_wrapper_names) == 12

    for unit, contract in units.items():
        content = (Path(__file__).parents[1] / "deploy/systemd" / unit).read_text(encoding="utf-8")
        stem = contract["stem"]
        assert directive_lines(content, "RequiresMountsFor") == (roots,)
        assert tuple(
            value for value in directive_values(content, "Environment") if value.startswith("HOME=")
        ) == (("HOME=%h" if unit == "termdeck-web-backend.service"
               else "HOME=/warp/projects/softwares/termdeck/.termdeck/state"),)
        assert tuple(line for line in content.splitlines() if line.startswith("Exec")) == (
            f"ExecStart=/warp/projects/softwares/termdeck/.termdeck/t1000-managed/{stem}",
            f"ExecStop=/warp/projects/softwares/termdeck/.termdeck/t1000-managed/{stem}-stop",
        )
        assert directive_values(content, "Restart") == ("on-failure",)
        assert directive_values(content, "RestartMode") == ("direct",)
        assert directive_values(content, "KillMode") == ("control-group",)

        # Wants starts and After orders related units; no hard Requires link
        # can cascade a TermDeck unit stop.
        assert directive_values(content, "Wants") == contract["wants"]
        assert directive_values(content, "After") == contract["after"]
        assert directive_values(content, "Requires") == ()

        expected_condition_directives = tuple(
            directive
            for directive, values in contract["conditions"].items()
            for _ in values
        )
        assert tuple(
            line.split("=", 1)[0] for line in content.splitlines() if line.startswith("Condition")
        ) == expected_condition_directives
        for directive, expected_values in contract["conditions"].items():
            assert directive_values(content, directive) == expected_values


def test_generated_canonical_watchdog_unit_exactly_matches_static_contract() -> None:
    paths = activation.ActivationPaths(
        target=Path("/warp/projects/softwares/termdeck"),
        evidence=Path("/warp/projects/softwares/termdeck/.termdeck/state/recovery/activation-v2"),
        runtime=Path("/warp/projects/softwares/termdeck/.termdeck"),
        database=Path("/warp/projects/softwares/termdeck/.termdeck/state/sessions.sqlite3"),
        managed_root=Path("/warp/projects/softwares/termdeck/.termdeck/t1000-managed"),
        managed_bin=Path("/warp/projects/softwares/termdeck/.termdeck/bin"),
        unit_dir=Path("/home/arch/.config/systemd/user"),
    )

    generated = activation._watchdog_unit(paths, paths.evidence / "tx-example")
    static = (Path(__file__).parents[1] / "deploy/systemd/termdeck-web-recovery.service").read_text(
        encoding="utf-8"
    )

    assert generated == static


def test_service_units_are_published_from_staged_transaction(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx = paths.evidence / "tx-1"
    staged = tx / "staged-units"
    staged.mkdir(parents=True)
    for unit in activation.SERVICES:
        (staged / unit).write_text(f"[Unit]\nDescription={unit}\n", encoding="utf-8")

    activation._publish_units(paths, tx)

    for unit in activation.SERVICES:
        assert (paths.unit_dir / unit).read_text(encoding="utf-8") == (
            f"[Unit]\nDescription={unit}\n"
        )


def test_custom_watchdog_command_and_write_scope_are_complete(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx = paths.evidence / "tx-1"
    unit = activation._watchdog_unit(paths, tx)

    properties = {
        key: value
        for line in unit.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    exec_args = shlex.split(properties["ExecStart"])
    assert exec_args == [str(paths.managed_root / "termdeck-web-recovery")]
    recovery_command = activation.service_wrappers.build_start_command(
        activation.WATCHDOG_UNIT,
        **activation._wrapper_kwargs(paths),
    )
    watchdog_index = recovery_command.index("--watchdog")
    assert recovery_command[watchdog_index + 1] == "--latest"
    assert "--latest" not in exec_args

    read_write_paths = set(shlex.split(properties["ReadWritePaths"]))
    for path in (
        paths.evidence,
        paths.database.parent,
        paths.runtime,
        paths.unit_dir,
        paths.managed_root,
        paths.managed_bin,
    ):
        assert str(path) in read_write_paths

    required_mounts = set(shlex.split(properties["RequiresMountsFor"]))
    for path in (paths.target, activation._wrapper_runtime_root(paths), paths.database.parent):
        assert str(path) in required_mounts
    assert required_mounts == {
        str(paths.target), str(activation._wrapper_runtime_root(paths)), str(paths.database.parent),
    }
    assert f"Environment=HOME={paths.database.parent}" in unit
    assert "ProtectSystem=strict" in unit


def test_watchdog_does_not_rollback_while_activation_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt.update(
        {
            "status": "mutation-start",
            "mutation_started": True,
            "heartbeat_epoch": time.time() - activation.WATCHDOG_TIMEOUT - 1,
        }
    )
    activation._write(tx / "receipt.json", receipt)

    @contextmanager
    def lock_is_held(_paths: activation.ActivationPaths):
        yield False

    class WatchdogStopped(Exception):
        pass

    monkeypatch.setattr(activation, "_try_activation_lock", lock_is_held)
    monkeypatch.setattr(
        activation,
        "rollback",
        lambda *args, **kwargs: pytest.fail("watchdog rolled back while activation lock was held"),
    )
    monkeypatch.setattr(
        activation.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(WatchdogStopped()),
    )

    with pytest.raises(WatchdogStopped):
        activation.watchdog(paths, tx)


@pytest.mark.parametrize("fresh_status", ["activated", "mutation-start"])
def test_watchdog_rechecks_receipt_after_lock_before_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_status: str,
) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    stale = dict(receipt)
    stale.update(
        {
            "status": "mutation-start",
            "mutation_started": True,
            "heartbeat_epoch": time.time() - activation.WATCHDOG_TIMEOUT - 1,
        }
    )
    fresh = dict(stale)
    fresh.update({"status": fresh_status, "heartbeat_epoch": time.time()})
    reads = [(tx, stale), (tx, fresh)]

    def reread(_paths: activation.ActivationPaths, _tx: Path):
        return reads.pop(0)

    class WatchdogStopped(Exception):
        pass

    monkeypatch.setattr(activation, "load_transaction", reread)
    # This fixture exercises only the second heartbeat read.  Real watchdog
    # workers must also prove their persisted armed identity before rollback;
    # that contract has dedicated stale-generation coverage below.
    monkeypatch.setattr(
        activation,
        "_watchdog_owns_current_armed_identity",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        activation,
        "rollback",
        lambda *args, **kwargs: pytest.fail("watchdog rolled back after a fresh receipt reread"),
    )
    monkeypatch.setattr(
        activation.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(WatchdogStopped()),
    )

    if fresh_status == "activated":
        assert activation.watchdog(paths, tx) == 0
    else:
        with pytest.raises(WatchdogStopped):
            activation.watchdog(paths, tx)
    assert reads == []


def test_watchdog_stale_latest_switch_after_lock_exits_without_service_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1 that resolved --latest before T2 must never roll back T2."""

    paths = _paths(tmp_path)
    source = {"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40}
    tx_one, receipt_one = activation.new_transaction(paths, source=source)
    tx_two, _receipt_two = activation.new_transaction(paths, source=source)
    receipt_one.update(
        {
            "status": "mutation-start",
            "mutation_started": True,
            "heartbeat_epoch": time.time() - activation.WATCHDOG_TIMEOUT - 1,
        }
    )
    activation._write(tx_one / "receipt.json", receipt_one)
    # Model systemd having resolved T1's --latest argument before activation
    # installed T2.  The competing activation wins while T1 waits for the
    # transaction lock, precisely where old code could stop T2's services.
    activation._write(
        paths.latest,
        {"schema": activation.SCHEMA, "transaction": tx_one.name, "source": source},
    )

    @contextmanager
    def lock_then_select_t2(_paths: activation.ActivationPaths):
        activation._write(
            paths.latest,
            {"schema": activation.SCHEMA, "transaction": tx_two.name, "source": source},
        )
        yield True

    mutations: list[str] = []
    monkeypatch.setattr(activation, "_try_activation_lock", lock_then_select_t2)
    monkeypatch.setattr(
        activation,
        "rollback",
        lambda *_args, **_kwargs: mutations.append("rollback"),
    )
    monkeypatch.setattr(
        activation,
        "_stop_web",
        lambda: mutations.append("stop-web"),
    )
    monkeypatch.setattr(
        activation,
        "_disarm",
        lambda: mutations.append("disarm"),
    )

    assert activation.watchdog(paths, tx_one) == 0
    assert mutations == []


@pytest.mark.parametrize(
    "case", ("published", "intent", "foreign-b", "recovered-b", "wrong-a", "changed-unit", "stale-latest")
)
def test_watchdog_accepts_only_exact_published_or_recovered_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A's watchdog follows B only through its own authenticated publication."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(paths, source={"commit": "c" * 40})
    old, new = "a" * 64, "b" * 64
    state = paths.managed_root / "state.json"
    wrapper = paths.managed_root / "termdeck-web-recovery"
    manifest = activation.service_wrappers.resolve_layout(**activation._wrapper_kwargs(paths)).manifest_path
    unit = paths.unit_dir / activation.WATCHDOG_UNIT
    journal = paths.managed_root / ".termdeck-publication.json"
    activation._write(state, {"current_release_id": old, "generation": 1})
    for path, content in ((wrapper, b"wrapper-a"), (manifest, b"manifest-a"), (unit, b"unit"), (journal, b"journal")):
        activation._atomic(path, content, 0o600)
    guards = release_manager._capture_publication_parent_guards((state, wrapper, manifest, unit, journal))

    def captures():
        return {
            path: activation._capture_anchored(path, label="successor fixture", parent_guards=guards)[1]
            for path in (state, wrapper, manifest)
        }

    before = captures()
    old_state = activation._watchdog_state_for_release(paths, old)
    execution = _fixture_wrapper_execution_identity
    armed = {
        "release_id": old,
        "state_generation": 1,
        "state_sha256": old_state["sha256"],
        "state_identity": old_state["identity"],
        "wrapper": activation._postimage_payload(wrapper, before[wrapper], guards),
        "manifest": activation._postimage_payload(manifest, before[manifest], guards),
        "wrapper_manifest_sha256": before[manifest].sha256,
        "unit": activation._postimage_payload(
            unit, activation._capture_anchored(unit, label="successor unit")[1], guards
        ),
        **execution(old),
    }
    activation._write(state, {"current_release_id": new, "generation": 2})
    activation._atomic(wrapper, b"wrapper-b", 0o600)
    activation._atomic(manifest, b"manifest-b", 0o600)
    after = captures()
    journal_identity = activation._capture_anchored(journal, label="successor journal")[1].identity
    publication = release_manager.PublicationTransactionReceipt(
        journal, journal_identity, "published", guards, before, after
    )
    receipt.update(
        old_release_id=old,
        package={"release_id": new},
        watchdog={"unit": activation.WATCHDOG_UNIT, "arm_phase": "pre-package", "expected_release_id": old, "armed_identity": armed},
        package_publication={
            "schema": activation.RELEASE_PUBLICATION_SCHEMA,
            "operation": "install",
            "expected_release_id": new,
            "state": "published",
            "receipt": activation._release_publication_receipt_payload(publication),
        },
    )
    monkeypatch.setattr(activation, "_wrapper_execution_identity", lambda _paths, release: execution(release))
    monkeypatch.setattr(
        activation, "_preflight_watchdog_wrapper",
        lambda _paths, release: {"manifest_sha256": after[manifest].sha256, **execution(release)},
    )
    if case == "intent":
        receipt["package_publication"]["state"] = "intent"
        receipt["package_publication"]["receipt"]["state"] = "intent"
    elif case in {"foreign-b", "recovered-b"}:
        for path, capture in after.items():
            activation._atomic(path, capture.snapshot.content, capture.snapshot.mode)
        if case == "recovered-b":
            recovery = release_manager.PublicationTransactionReceipt(
                journal, journal_identity, "recovered", guards, after, captures()
            )
            receipt["package_publication"]["recovery"] = activation._release_publication_receipt_payload(recovery)
    elif case == "wrong-a":
        armed["state_identity"] = {**armed["state_identity"], "inode": armed["state_identity"]["inode"] + 1}
    elif case == "changed-unit":
        activation._atomic(unit, b"unit", 0o600)
    elif case == "stale-latest":
        activation.new_transaction(paths, source={"commit": "d" * 40})

    assert activation._watchdog_owns_current_armed_identity(paths, tx, receipt) is (
        case in {"published", "recovered-b"}
    )


def test_watchdog_rejects_stale_armed_state_generation_before_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted generation is a required ownership check, not metadata."""

    paths = _paths(tmp_path)
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    identity = {
        "device": 1,
        "inode": 2,
        "mode": 0o600,
        "nlink": 1,
        "size": 3,
        "mtime_ns": 4,
        "ctime_ns": 5,
    }
    armed = {
        "release_id": "a" * 64,
        "state_generation": 6,
        "state_sha256": "a" * 64,
        "state_identity": identity,
        "wrapper": {},
        "manifest": {},
        "wrapper_manifest_sha256": "b" * 64,
        "unit": {},
    }
    receipt = {
        "watchdog": {
            "unit": activation.WATCHDOG_UNIT,
            "expected_release_id": "a" * 64,
            "armed_identity": armed,
        }
    }
    monkeypatch.setattr(activation, "_watchdog_transaction_is_latest", lambda *_args: True)
    monkeypatch.setattr(
        activation,
        "_watchdog_state_for_release",
        lambda *_args: {
            "release_id": "a" * 64,
            "generation": 7,
            "sha256": "a" * 64,
            "identity": identity,
        },
    )
    monkeypatch.setattr(
        activation,
        "_assert_current_postimage",
        lambda *_args, **_kwargs: pytest.fail("stale generation reached leaf/service checks"),
    )

    assert not activation._watchdog_owns_current_armed_identity(paths, tx, receipt)


def test_watchdog_rejects_checkout_binding_drift_before_service_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale watchdog cannot use a re-bound wrapper execution context."""

    paths = _paths(tmp_path)
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    release_id = "a" * 64
    identity = {
        "device": 1,
        "inode": 2,
        "mode": 0o600,
        "nlink": 1,
        "size": 3,
        "mtime_ns": 4,
        "ctime_ns": 5,
    }
    armed = {
        "release_id": release_id,
        "state_generation": 7,
        "state_sha256": "a" * 64,
        "state_identity": identity,
        "wrapper": {},
        "manifest": {},
        "wrapper_manifest_sha256": "b" * 64,
        "unit": {},
        **_fixture_wrapper_execution_identity(release_id),
    }
    receipt = {
        "watchdog": {
            "unit": activation.WATCHDOG_UNIT,
            "expected_release_id": release_id,
            "armed_identity": armed,
        }
    }
    monkeypatch.setattr(activation, "_watchdog_transaction_is_latest", lambda *_args: True)
    monkeypatch.setattr(
        activation,
        "_watchdog_state_for_release",
        lambda *_args: {
            "release_id": release_id,
            "generation": 7,
            "sha256": "a" * 64,
            "identity": identity,
        },
    )
    drifted = _fixture_wrapper_execution_identity(release_id)
    drifted["execution_root"] = "/foreign/releases/a/site"
    monkeypatch.setattr(activation, "_wrapper_execution_identity", lambda *_args: drifted)
    monkeypatch.setattr(
        activation,
        "_assert_current_postimage",
        lambda *_args, **_kwargs: pytest.fail("checkout drift reached leaf/service checks"),
    )

    assert not activation._watchdog_owns_current_armed_identity(paths, tx, receipt)


def test_package_effect_before_receipt_ack_is_rolled_back_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from termdeck import legacy_activation
    monkeypatch.setattr(legacy_activation, "prepare_recovery", lambda *_args: None)
    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    old_release = "a" * 64
    new_release = "b" * 64
    activation._write(
        paths.managed_root / "state.json",
        {"current_release_id": old_release},
    )
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt["status"] = "artifacts-staged"
    receipt["package"] = {
        "wheel": "qualified.whl",
        "manifest": "qualified.release.json",
        "release_id": new_release,
    }
    activation._write(tx / "receipt.json", receipt)

    class FakeThread:
        instances: list["FakeThread"] = []

        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False
            self.joined = False
            self.join_calls = 0
            self.instances.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.joined

        def join(self):
            self.join_calls += 1
            self.joined = True

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda paths, tx, label: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda paths, tx: {})
    monkeypatch.setattr(activation, "_arm_and_record_watchdog", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *args, check=True: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(activation, "_publish_units", lambda paths, tx, **_kwargs: None)
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(activation, "_stop_web", lambda: None)
    monkeypatch.setattr(activation, "_disarm", lambda: None)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda paths, tx, before: {
            "integrity": "ok",
            "lost_session_ids": [],
            "preserved_session_ids": 0,
        },
    )
    rollback_targets: list[object] = []

    def effect_then_crash(paths, package, **_kwargs):
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": new_release},
        )
        raise RuntimeError("crash after package effect before receipt ACK")

    def restore_package(paths, release, **_kwargs):
        rollback_targets.append(release)
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": old_release},
        )
        return {"ok": True}

    monkeypatch.setattr(activation, "_install", effect_then_crash)
    monkeypatch.setattr(activation, "_rollback_package", restore_package)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_assert_package_rollback_owned", lambda *_args, **_kwargs: None)

    persist_statuses: list[str] = []
    real_persist = activation._persist

    def tracked_persist(paths, tx, receipt, status=None):
        if status is not None:
            persist_statuses.append(status)
            if status == "failed":
                assert FakeThread.instances[0].joined is True
        return real_persist(paths, tx, receipt, status)

    monkeypatch.setattr(activation, "_persist", tracked_persist)

    with pytest.raises(RuntimeError, match="crash after package effect"):
        activation.execute(paths, tx)

    final = activation._read(tx / "receipt.json")
    assert rollback_targets == [old_release]
    assert activation._read(paths.managed_root / "state.json")["current_release_id"] == old_release
    assert final["status"] == "rolled-back"
    assert final["package_rollback_required"] is True
    assert final["package_current_release_id"] == new_release
    assert "package-installed" not in persist_statuses
    assert FakeThread.instances[0].join_calls == 1


def test_real_a_to_b_post_install_failure_rolls_back_before_outer_wrapper_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real release-manager rollback while B wrappers are live."""

    paths = _integration_paths(tmp_path)
    # Authenticate an existing committed A that has no lifecycle handoff
    # implementation. Recovery must execute B's capsule before any A config
    # is changed; rebuilding A with current wrapper code would miss this gap.
    project = Path(__file__).parents[1]
    old_revision = "26913eb70ce391bf59317b5d254de99b0d55198d"
    old_wrappers = subprocess.check_output(["git", "show", old_revision + ":termdeck/service_wrappers.py"], cwd=project)
    assert b"def _publish_activation_config_transition" not in old_wrappers
    wheel_a, manifest_a, release_a = _release_artifact(tmp_path, "a", module_overrides={"termdeck/service_wrappers.py": old_wrappers})
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    monkeypatch.setattr(
        release_manager,
        "_require_managed_storage",
        lambda _layout: _storage_attestation(),
    )
    release_manager.install_release(
        wheel_a,
        manifest_a,
        root=paths.managed_root,
        bin_dir=paths.managed_bin,
        python=sys.executable,
        production=True,
    )
    old_release_id = str(release_a["release_id"])
    new_release_id = str(release_b["release_id"])
    wrapper_preimage = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in activation._wrapper_publication_paths(paths)
    }
    assert len(wrapper_preimage) == 19

    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": str(wheel_b),
                "manifest": str(manifest_b),
                "release_id": new_release_id,
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)
    _inject_post_install_failure(monkeypatch)

    with pytest.raises(RuntimeError, match="post-install activation failure"):
        activation.execute(paths, tx)

    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    assert final["package_rollback_required"] is True
    assert final["package_current_release_id"] == new_release_id
    assert final["old_release_id"] == old_release_id
    assert activation._managed_release_id(paths) == old_release_id
    assert not paths.recovery_required.exists()
    watchdog = final["watchdog"]
    assert isinstance(watchdog, dict)
    assert watchdog["arm_phase"] == "recovery-capsule"
    assert final["recovery_capsule"]["original_state"]["release_id"] == old_release_id
    history = final["phase_history"]
    assert isinstance(history, list)
    assert history.index("watchdog-armed") < history.index("package-installing")
    assert {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in wrapper_preimage
    } == wrapper_preimage
    # Release trees are immutable rollback history.  B remains verified but is
    # no longer selected after the activation transaction restores A.
    assert release_manager.verify_installed_tree(paths.managed_root / "releases" / new_release_id)["ok"]


def test_real_first_install_post_install_failure_restores_empty_package_and_wrapper_preimages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo a first install through the authenticated empty-legacy API."""

    paths = _integration_paths(tmp_path)
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    layout = release_manager.release_layout(
        root=paths.managed_root,
        bin_dir=paths.managed_bin,
    )
    wrapper_preimage = activation._wrapper_publication_paths(paths)
    package_preimage = (
        layout.bootstrap_path,
        layout.state_path,
        layout.launcher_path,
        layout.legacy_dir / "service-wrapper-preimage.json",
        layout.legacy_dir / "first-install-preimage.json",
    )
    assert all(
        not os.path.lexists(path)
        for path in (*wrapper_preimage, *package_preimage)
    )
    monkeypatch.setattr(
        release_manager,
        "_require_managed_storage",
        lambda _layout: _storage_attestation(),
    )
    new_release_id = str(release_b["release_id"])
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": str(wheel_b),
                "manifest": str(manifest_b),
                "release_id": new_release_id,
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)
    _inject_post_install_failure(monkeypatch)

    with pytest.raises(RuntimeError, match="post-install activation failure"):
        activation.execute(paths, tx)

    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    assert final["package_rollback_required"] is True
    assert final["package_current_release_id"] == new_release_id
    assert final["old_release_id"] is None
    assert activation._managed_release_id(paths) is None
    assert not paths.recovery_required.exists()
    watchdog = final["watchdog"]
    assert isinstance(watchdog, dict)
    assert watchdog["arm_phase"] == "post-package"
    assert watchdog["armed_identity"]["release_id"] == new_release_id
    history = final["phase_history"]
    assert isinstance(history, list)
    assert history.index("package-installed") < history.index("watchdog-installing")
    journal = final["publication_journal"]
    assert isinstance(journal, list)
    journal_records = [entry["record"] for entry in journal]
    assert journal_records.index("wrapper:managed-release") < journal_records.index(
        "unit:" + activation.WATCHDOG_UNIT
    )
    # The release manager authenticated B against its live wrapper set, then
    # restored the first-install absent baseline.  The outer transaction's
    # same absent wrapper preimages are consequently safe to replay.
    assert all(
        not os.path.lexists(path)
        for path in (*wrapper_preimage, *package_preimage)
    )
    # Immutable release trees are audit history rather than live selection.
    assert release_manager.verify_installed_tree(paths.managed_root / "releases" / new_release_id)["ok"]


@pytest.mark.parametrize("prior_release", (True, False), ids=("a-to-b", "first-install"))
def test_real_release_callback_baseexception_recovers_exact_package_preimages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_release: bool,
) -> None:
    """A crash after callback persistence leaves no unowned B generation.

    This is deliberately a ``BaseException`` rather than an ordinary error:
    it bypasses execute's in-process compensation and makes rollback start as
    a fresh process would.  The retained release-manager journal must restore
    its exact starting set before activation decides whether any explicit
    package inverse is still needed.
    """

    paths = _integration_paths(tmp_path)
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    monkeypatch.setattr(
        release_manager,
        "_require_managed_storage",
        lambda _layout: _storage_attestation(),
    )
    old_release_id: str | None = None
    if prior_release:
        wheel_a, manifest_a, release_a = _release_artifact(tmp_path, "a")
        release_manager.install_release(
            wheel_a,
            manifest_a,
            root=paths.managed_root,
            bin_dir=paths.managed_bin,
            python=sys.executable,
            production=True,
        )
        old_release_id = str(release_a["release_id"])

    layout = release_manager.release_layout(
        root=paths.managed_root,
        bin_dir=paths.managed_bin,
    )
    package_paths = (
        layout.bootstrap_path,
        layout.state_path,
        layout.launcher_path,
        layout.legacy_dir / "service-wrapper-preimage.json",
        layout.legacy_dir / "first-install-preimage.json",
    )
    tracked_paths = (*activation._wrapper_publication_paths(paths), *package_paths)
    preimage = {path: _file_preimage(path) for path in tracked_paths}

    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": str(wheel_b),
                "manifest": str(manifest_b),
                "release_id": str(release_b["release_id"]),
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)
    _inject_post_install_failure(monkeypatch)

    class CallbackCrash(BaseException):
        pass

    real_checkpoint = activation._checkpoint_publication

    def crash_after_durable_callback(
        paths: activation.ActivationPaths,
        transaction: Path,
        value: dict[str, object],
        record: str,
    ) -> None:
        real_checkpoint(paths, transaction, value, record)
        if record == "install:publication-postimage":
            raise CallbackCrash("after durable release callback postimage")

    monkeypatch.setattr(
        activation,
        "_checkpoint_publication",
        crash_after_durable_callback,
    )

    with pytest.raises(CallbackCrash, match="durable release callback postimage"):
        activation.execute(paths, tx)

    durable = activation._read(tx / "receipt.json")
    publication = durable["package_publication"]
    assert isinstance(publication, dict)
    assert publication["state"] == "published"
    handoff = publication["receipt"]
    assert isinstance(handoff, dict)
    # release_manager catches BaseException around its descriptor journal and
    # performs the same authenticated inverse before re-raising.  A new
    # activation process still sees the callback receipt, but never an
    # unowned B state or a discarded journal with B left public.
    assert not os.path.lexists(Path(str(handoff["journal_path"])))
    assert activation._managed_release_id(paths) == old_release_id

    result = activation.rollback(paths, tx, reason="simulated callback process death")

    assert result["action"] == "rolled-back"
    assert activation._managed_release_id(paths) == old_release_id
    assert {path: _file_preimage(path) for path in tracked_paths} == preimage
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    assert not paths.recovery_required.exists()
    # A terminal retry must be a no-op even after the release journal recovery
    # already performed the authenticated inverse.
    repeated = activation.rollback(paths, tx, reason="repeat terminal rollback")
    assert repeated["action"] == "rolled-back"
    assert {path: _file_preimage(path) for path in tracked_paths} == preimage


@pytest.mark.parametrize("prior_release", (True, False), ids=("a-to-b", "first-install"))
def test_real_release_callback_process_death_recovers_retained_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_release: bool,
) -> None:
    """A fresh rollback process resolves an exact B journal after hard exit."""

    paths = _integration_paths(tmp_path)
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    monkeypatch.setattr(
        release_manager,
        "_require_managed_storage",
        lambda _layout: _storage_attestation(),
    )
    old_release_id: str | None = None
    if prior_release:
        wheel_a, manifest_a, release_a = _release_artifact(tmp_path, "a")
        release_manager.install_release(
            wheel_a,
            manifest_a,
            root=paths.managed_root,
            bin_dir=paths.managed_bin,
            python=sys.executable,
            production=True,
        )
        old_release_id = str(release_a["release_id"])

    layout = release_manager.release_layout(root=paths.managed_root, bin_dir=paths.managed_bin)
    package_paths = (
        layout.bootstrap_path,
        layout.state_path,
        layout.launcher_path,
        layout.legacy_dir / "service-wrapper-preimage.json",
        layout.legacy_dir / "first-install-preimage.json",
    )
    tracked_paths = (*activation._wrapper_publication_paths(paths), *package_paths)
    preimage = {path: _file_preimage(path) for path in tracked_paths}
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": str(wheel_b),
                "manifest": str(manifest_b),
                "release_id": str(release_b["release_id"]),
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)

    crashed = _hard_crash_after_release_callback(paths, tx)
    assert crashed.returncode == 86, crashed.stderr
    durable = activation._read(tx / "receipt.json")
    publication = durable["package_publication"]
    assert isinstance(publication, dict)
    assert publication["state"] == "published"
    handoff = publication["receipt"]
    assert isinstance(handoff, dict)
    assert os.path.lexists(Path(str(handoff["journal_path"])))
    assert activation._managed_release_id(paths) == str(release_b["release_id"])

    _inject_post_install_failure(monkeypatch)
    result = activation.rollback(paths, tx, reason="fresh-process callback crash recovery")

    assert result["action"] == "rolled-back"
    assert activation._managed_release_id(paths) == old_release_id
    assert {path: _file_preimage(path) for path in tracked_paths} == preimage
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    assert not paths.recovery_required.exists()
    assert activation.rollback(paths, tx, reason="repeat terminal rollback")["action"] == "rolled-back"


@pytest.mark.parametrize("recovery_case", ("single", "callback-replay", "second-inverse"))
def test_hard_death_after_inverse_callback_retries_distinct_release_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_case: str,
) -> None:
    """A retained A-to-B journal cannot be confused with a new B-to-A one."""

    paths = _integration_paths(tmp_path)
    wheel_a, manifest_a, release_a = _release_artifact(tmp_path, "a")
    wheel_b, manifest_b, release_b = _release_artifact(tmp_path, "b")
    monkeypatch.setattr(
        release_manager,
        "_require_managed_storage",
        lambda _layout: _storage_attestation(),
    )
    release_manager.install_release(
        wheel_a,
        manifest_a,
        root=paths.managed_root,
        bin_dir=paths.managed_bin,
        python=sys.executable,
        production=True,
    )
    old_release_id = str(release_a["release_id"])
    new_release_id = str(release_b["release_id"])
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": str(wheel_b),
                "manifest": str(manifest_b),
                "release_id": new_release_id,
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)

    # First process dies only after RM committed B and activation durably
    # checkpointed that exact B generation.  This is deliberately beyond the
    # forward-journal recovery point, so the next process really must create
    # an inverse transaction rather than have RM restore A automatically.
    forward = _hard_crash_after_release_commit(paths, tx)
    assert forward.returncode == 88, forward.stderr
    durable = activation._read(tx / "receipt.json")
    package_publication = durable["package_publication"]
    assert isinstance(package_publication, dict)
    forward_handoff = package_publication["receipt"]
    assert isinstance(forward_handoff, dict)
    assert activation._managed_release_id(paths) == new_release_id

    # The next process performs the authenticated inverse and dies after its
    # *separate* rollback callback receipt was durably checkpointed.
    inverse = _hard_crash_after_release_inverse_callback(paths, tx)
    assert inverse.returncode == 87, inverse.stderr
    durable = activation._read(tx / "receipt.json")
    inverse_publication = durable["package_rollback_publication"]
    assert isinstance(inverse_publication, dict)
    inverse_handoff = inverse_publication["receipt"]
    assert isinstance(inverse_handoff, dict)
    assert forward_handoff["journal_path"] != inverse_handoff["journal_path"]
    assert str(inverse_handoff["journal_path"]).endswith(
        ".termdeck-publication-rollback.json"
    )
    # The callback completed A selection before the forced process death.
    assert activation._managed_release_id(paths) == old_release_id

    if recovery_case == "second-inverse":
        # Recovery may restore B with new descriptor identities before a
        # second inverse crashes.  The next recovery must chain from that B.
        repeated_inverse = _hard_crash_after_release_inverse_callback(paths, tx)
        assert repeated_inverse.returncode == 87, repeated_inverse.stderr
        assert activation._managed_release_id(paths) == old_release_id

    recovery_callbacks: list[object] = []
    if recovery_case == "callback-replay":
        recover = release_manager.recover_install_publication

        def recover_with_callback_replay(*args, **kwargs):
            callback = kwargs.get("after_recovery")
            assert callback is not None

            def replay(publication):
                callback(publication)
                recovery_callbacks.append(publication)
                # First callback already persisted the outer recovered B
                # identities.  A replay must accept that exact same event.
                callback(publication)

            kwargs["after_recovery"] = replay
            return recover(*args, **kwargs)

        monkeypatch.setattr(
            release_manager, "recover_install_publication", recover_with_callback_replay
        )

    _inject_post_install_failure(monkeypatch)
    result = activation.rollback(paths, tx, reason="retry inverse hard death")

    assert result["action"] == "rolled-back"
    assert activation._managed_release_id(paths) == old_release_id
    assert not paths.recovery_required.exists()
    assert activation.rollback(paths, tx, reason="repeat inverse retry")["action"] == "rolled-back"
    if recovery_case == "callback-replay":
        assert recovery_callbacks


def test_execute_publishes_units_before_stopping_web_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from termdeck import legacy_activation
    monkeypatch.setattr(legacy_activation, "prepare_recovery", lambda *_args: None)
    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    old_release = "a" * 64
    new_release = "b" * 64
    activation._write(paths.managed_root / "state.json", {"current_release_id": old_release})
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt["status"] = "artifacts-staged"
    receipt["package"] = {
        "wheel": "qualified.whl",
        "manifest": "qualified.release.json",
        "release_id": new_release,
    }
    activation._write(tx / "receipt.json", receipt)

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda paths, tx, label: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda paths, tx: {})
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *args, check=True: SimpleNamespace(returncode=0),
    )
    events: list[str] = []

    def record_arm(*args, **_kwargs) -> None:
        receipt = args[2]
        release_id = args[4]
        receipt["watchdog"] = {
            "unit": activation.WATCHDOG_UNIT,
            "armed_before_disruption": True,
            "expected_release_id": release_id,
        }
        events.append("arm:" + release_id)

    monkeypatch.setattr(activation, "_arm_and_record_watchdog", record_arm)
    real_persist = activation._persist

    def tracked_persist(paths, transaction, value, status=None):
        if status is not None:
            events.append("persist:" + status)
        return real_persist(paths, transaction, value, status)

    monkeypatch.setattr(activation, "_persist", tracked_persist)
    monkeypatch.setattr(
        activation,
        "_publish_units",
        lambda paths, tx, **_kwargs: events.append("publish-units"),
    )
    monkeypatch.setattr(
        activation,
        "_publish_wrappers",
        lambda paths, release_id: events.append("publish-wrappers") or {"ok": True, "release_id": release_id},
    )
    monkeypatch.setattr(
        activation,
        "_capture_wrapper_postimages",
        lambda *_args, **_kwargs: {"ok": True, "release_id": new_release},
    )
    monkeypatch.setattr(
        activation,
        "_install",
        lambda paths, package, **_kwargs: (
            events.append("install:" + new_release)
            or
            activation._write(
                paths.managed_root / "state.json",
                {"current_release_id": new_release},
            )
            or {"ok": True, "release_id": new_release}
        ),
    )
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    def stop_web() -> None:
        # The receipt must only claim stopped services after the bounded stop
        # routine has actually returned successfully.
        assert activation._read(tx / "receipt.json").get("services_stopped") is not True
        events.append("stop-web")

    monkeypatch.setattr(activation, "_stop_web", stop_web)
    monkeypatch.setattr(activation, "_start_web", lambda _paths: events.append("start-web"))
    monkeypatch.setattr(
        activation,
        "_capture_generated_runtime_postimages",
        lambda *_args, **_kwargs: events.append("capture-generated-runtime"),
    )
    monkeypatch.setattr(activation, "_health", lambda: {"backend": "ok"})
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda paths, tx, before: {"integrity": "ok", "lost_session_ids": []},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda before: {"disappeared": [], "natural_exit_allowed": True},
    )
    monkeypatch.setattr(activation, "_disarm", lambda: events.append("disarm"))

    result = activation.execute(paths, tx)

    assert result["action"] == "activated"
    assert events.index("arm:" + old_release) < events.index("install:" + new_release)
    assert events.index("publish-wrappers") < events.index("stop-web")
    assert events.index("publish-units") < events.index("stop-web")
    assert events.index("start-web") < events.index("capture-generated-runtime")
    assert events.index("disarm") < events.index("persist:activated")
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "activated"
    assert final["service_units_published"] is True
    assert final["wrappers_published"] is True


def test_outer_atomic_write_refuses_parent_swap_without_touching_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed activation parent cannot redirect an outer config write."""

    parent = tmp_path / "activation-parent"
    parent.mkdir()
    target = parent / "outer.conf"
    target.write_bytes(b"before")
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir()
    foreign = foreign_parent / target.name
    foreign.write_bytes(b"foreign")
    displaced_parent = tmp_path / "displaced-parent"
    original_probe = release_manager._atomic_write_bytes
    swapped = False

    def swap_parent(path: Path, content: bytes, *, mode: int = 0o644, **kwargs: object) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            parent.rename(displaced_parent)
            parent.symlink_to(foreign_parent, target_is_directory=True)
        original_probe(path, content, mode=mode, **kwargs)

    monkeypatch.setattr(release_manager, "_atomic_write_bytes", swap_parent)

    with pytest.raises(activation.WebActivationError, match="parent identity|not a real directory|symlink"):
        activation._atomic(target, b"new", 0o600)

    assert foreign.read_bytes() == b"foreign"
    assert (displaced_parent / target.name).read_bytes() == b"before"


def test_outer_restore_refuses_leaf_swap_without_overwriting_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback restores only the leaf that its transaction actually wrote."""

    parent = tmp_path / "activation-parent"
    parent.mkdir()
    target = parent / "outer.conf"
    target.write_bytes(b"before")
    os.chmod(target, 0o640)
    tx = tmp_path / "transaction"
    tx.mkdir()
    record = activation._preimage(target, tx, "outer.conf")
    activation._publish_preimaged(
        record,
        tx,
        b"transaction-owned-postimage",
        0o600,
        label="test outer config",
    )
    foreign = tmp_path / "foreign.conf"
    foreign.write_bytes(b"foreign")
    os.chmod(foreign, 0o644)
    original_probe = release_manager._atomic_write_bytes
    swapped = False

    def swap_leaf(path: Path, content: bytes, *, mode: int = 0o644, **kwargs: object) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            os.replace(foreign, target)
        original_probe(path, content, mode=mode, **kwargs)

    monkeypatch.setattr(release_manager, "_atomic_write_bytes", swap_leaf)

    with pytest.raises(activation.WebActivationError, match="changed before commit"):
        activation._restore(record, tx)

    assert target.read_bytes() == b"foreign"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_generated_runtime_postimages_are_durable_and_rollback_owned(
    tmp_path: Path,
) -> None:
    """Adopt each synchronous LAN-prep output before any health boundary."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages: dict[str, dict[str, object]] = {}
    receipt["preimages"] = preimages
    for relative in activation.GENERATED_RUNTIME:
        preimages["generated:" + relative] = activation._preimage(
            paths.runtime / relative,
            tx,
            "generated-" + relative.replace("/", "-"),
        )
    receipt["mutation_started"] = True
    activation._persist(paths, tx, receipt, "services-starting")

    _write_runtime_preparer_outputs(paths)

    activation._capture_generated_runtime_postimages(
        paths,
        tx,
        preimages,
        checkpoint=lambda record: activation._checkpoint_publication(paths, tx, receipt, record),
    )

    durable = activation._read(tx / "receipt.json")
    journal = durable["publication_journal"]
    assert isinstance(journal, list)
    assert [entry["record"] for entry in journal] == (
        ["generated:tls-publication"]
        + ["generated:" + relative for relative in activation.GENERATED_RUNTIME]
    )
    durable_preimages = durable["preimages"]
    assert isinstance(durable_preimages, dict)
    for relative in activation.GENERATED_RUNTIME:
        record = durable_preimages["generated:" + relative]
        assert isinstance(record, dict)
        assert isinstance(record["postimage"], dict)
        assert activation._restore(record, tx) == "restored"
        assert not os.path.lexists(paths.runtime / relative)


def test_generated_runtime_refuses_unbound_regular_files(
    tmp_path: Path,
) -> None:
    """Only the preparer's final v2 receipt may make visible runtime B owned."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages: dict[str, dict[str, object]] = {}
    receipt["preimages"] = preimages
    for relative in activation.GENERATED_RUNTIME:
        preimages["generated:" + relative] = activation._preimage(
            paths.runtime / relative,
            tx,
            "generated-unbound-" + relative.replace("/", "-"),
        )

    activation._atomic(paths.runtime / "current-lan-ip", b"192.168.1.42\n", 0o600)
    activation._atomic(paths.runtime / "nginx/allowed-hosts.map", b"192.168.1.42 1;\n", 0o600)
    activation._atomic(paths.runtime / "build-receipt.json", b"{}\n", 0o644)

    with pytest.raises(activation.WebActivationError, match="runtime receipt schema"):
        activation._capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=lambda _record: pytest.fail("unbound runtime output was adopted"),
        )

    assert all(record["postimage"] is None for record in preimages.values())
    assert (paths.runtime / "current-lan-ip").read_bytes() == b"192.168.1.42\n"


def test_generated_runtime_refuses_forged_v2_producer_attestation(
    tmp_path: Path,
) -> None:
    """A syntactically faithful receipt still needs a valid producer signature."""

    paths = _paths(tmp_path)
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages = _generated_runtime_preimages(paths, tx)
    _write_runtime_preparer_outputs(paths)

    receipt_path = paths.runtime / "build-receipt.json"
    forged = json.loads(receipt_path.read_text(encoding="utf-8"))
    response = forged["producer_attestation"]
    response["attestation"]["signature"] = base64.urlsafe_b64encode(b"\0" * 64).rstrip(b"=").decode("ascii")
    _rewrite_regular_same_inode(
        receipt_path,
        (json.dumps(forged, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )

    with pytest.raises(activation.WebActivationError, match="attestation|signature"):
        activation._capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=lambda _record: pytest.fail("forged runtime receipt was adopted"),
        )
    assert all(record["postimage"] is None for record in preimages.values())


def test_runtime_attestation_verifies_real_signature_with_only_public_key(
    tmp_path: Path,
) -> None:
    """A persisted broker response verifies without access to its signing key."""

    paths = _paths(tmp_path)
    _write_runtime_preparer_outputs(paths)
    config = _test_attestation_config(paths)
    record = json.loads((config.records_path / ("a" * 32 + ".json")).read_text())
    receipt = json.loads(config.runtime_receipt_path.read_text())
    assert record["response"] == receipt["producer_attestation"]
    config.private_key_path.unlink()

    with runtime_attestation.open_verified_runtime_receipt(config=config) as lease:
        assert lease.receipt == receipt
        leaf = lease.open_runtime_leaf("current-lan-ip")
        try:
            assert os.read(leaf, 128) == b"192.168.1.42\n"
        finally:
            os.close(leaf)


@pytest.mark.parametrize("root_identity", ("uid", "gid"))
def test_runtime_production_attestor_rejects_root_preparer(
    monkeypatch: pytest.MonkeyPatch, root_identity: str
) -> None:
    """Production cannot collapse the signing authority into its requester."""

    preparer_uid = 0 if root_identity == "uid" else 1201
    preparer_gid = 0 if root_identity == "gid" else 1202
    accounts = {
        "termdeck-preparer": SimpleNamespace(
            pw_name="termdeck-preparer", pw_uid=preparer_uid, pw_gid=preparer_gid
        ),
        "max": SimpleNamespace(pw_name="max", pw_uid=1000, pw_gid=1000),
    }
    group = SimpleNamespace(gr_gid=preparer_gid, gr_mem=[])
    monkeypatch.setattr(runtime_attestation.pwd, "getpwnam", accounts.__getitem__)
    monkeypatch.setattr(runtime_attestation.grp, "getgrnam", lambda _name: group)
    monkeypatch.setattr(runtime_attestation.grp, "getgrall", lambda: [group])
    monkeypatch.setattr(runtime_attestation.os, "getgrouplist", lambda _name, gid: [gid])

    with pytest.raises(runtime_attestation.AttestationError, match="separate from root"):
        runtime_attestation.production_config()


def test_runtime_attestation_rejects_receipt_swap_during_signature_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The returned lease must still name the receipt its signature verified."""

    paths = _paths(tmp_path)
    _write_runtime_preparer_outputs(paths)
    config = _test_attestation_config(paths)
    replacement = paths.runtime / "replacement-receipt.json"
    replacement.write_bytes(config.runtime_receipt_path.read_bytes())
    replacement.chmod(runtime_attestation.RECEIPT_MODE)
    foreign_inode = replacement.stat().st_ino
    verify = runtime_attestation.verify_runtime_build_attestation

    def verify_then_swap(receipt: object, *, config):
        verified = verify(receipt, config=config)
        os.replace(replacement, config.runtime_receipt_path)
        return verified

    monkeypatch.setattr(
        runtime_attestation, "verify_runtime_build_attestation", verify_then_swap
    )

    with pytest.raises(runtime_attestation.AttestationError, match="changed after attestation|attestation leaf is not a regular file"):
        runtime_attestation.open_verified_runtime_receipt(config=config)

    assert config.runtime_receipt_path.stat().st_ino == foreign_inode


@pytest.mark.parametrize("rewrite_key", (False, True), ids=("unchanged", "late-key-rewrite"))
def test_runtime_tls_validation_retains_leaf_identity_through_final_parent_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewrite_key: bool
) -> None:
    """Late same-inode key writes cannot escape the final TLS descriptor fence."""

    paths = _paths(tmp_path)
    _write_runtime_preparer_outputs(paths)
    receipt = json.loads((paths.runtime / "build-receipt.json").read_text())
    key = paths.runtime / "tls/server.key"
    key_inode = key.stat().st_ino
    parent_open = activation._open_runtime_key_parent
    file_open = os.open
    parent_checks = 0
    leaf_descriptors: list[int] = []

    def track_open(path, flags, *args, **kwargs):
        descriptor = file_open(path, flags, *args, **kwargs)
        if path == "server.key":
            leaf_descriptors.append(descriptor)
        return descriptor

    def checked_parent(path: Path):
        nonlocal parent_checks
        result = parent_open(path)
        parent_checks += 1
        if rewrite_key and parent_checks == 2:
            _rewrite_regular_same_inode(key, b"late-unattested-key\n")
        return result

    monkeypatch.setattr(activation.os, "open", track_open)
    monkeypatch.setattr(activation, "_open_runtime_key_parent", checked_parent)

    if rewrite_key:
        with pytest.raises(activation.WebActivationError, match="TLS.*changed"):
            activation._runtime_tls_publication(paths, receipt["tls_publication"])
    else:
        assert activation._runtime_tls_publication(paths, receipt["tls_publication"]) == receipt["tls_publication"]

    assert parent_checks == 2
    assert key.stat().st_ino == key_inode
    assert leaf_descriptors
    for descriptor in leaf_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    "case",
    ("complete", "missing-field", "wrong-output", "wrong-receipt", "no-prior", "crash-before-result", "foreign-replay"),
)
def test_runtime_inverse_authenticates_complete_response_and_exact_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Producer replies cannot replace signed A or the outer activation preimages."""

    paths = _paths(tmp_path)
    _write_runtime_preparer_outputs(paths)
    activation._atomic(paths.runtime / "bin/termdeck-web-prepare", b"#!/bin/sh\n", 0o755)
    tx, receipt = activation.new_transaction(paths, source={"commit": "c" * 40})
    preimages = _generated_runtime_preimages(paths, tx)
    receipt.update(preimages=preimages, phase_history=["services-starting"])
    preimages["generated:build-receipt.json"]["runtime_rollback_intent"] = {
        "schema": "termdeck.web-activation-runtime-inverse.v1",
        "mode": "discover",
        "generation": None,
    }
    activation._write(tx / "receipt.json", receipt)
    public_receipt = json.loads((paths.runtime / "build-receipt.json").read_text())

    def claim(relative: str):
        path = paths.runtime / relative
        return {
            "identity": list(activation._runtime_tls_leaf_identity(path.stat(), label="inverse fixture")),
            "sha256": sha256_file(path),
        }

    # The subprocess boundary is isolated; restored A remains a real attested
    # publication and is checked against exact original activation preimages.
    current_tls = public_receipt["tls_publication"]["current"]
    response = {
        "ok": True,
        "action": "restored-runtime-generation",
        "generation": "b" * 32,
        "discovered": True,
        "receipt": {"path": "build-receipt.json", **claim("build-receipt.json")},
        "restored": {
            "outputs": {relative: claim(relative) for relative in activation.RUNTIME_PREPARER_OUTPUTS},
            "runtime_generation": "a" * 32,
            "tls": current_tls,
        },
        "retained": {
            "journal": ".termdeck-tls-transaction-" + "b" * 32,
            "journal_identity": current_tls["identity"],
            "path": "prior-tls",
            "identity": current_tls["identity"],
            "manifest": current_tls["manifest"],
        },
    }
    if case == "missing-field":
        response.pop("receipt")
    elif case == "wrong-output":
        response["restored"]["outputs"]["current-lan-ip"]["sha256"] = "0" * 64
    elif case == "wrong-receipt":
        response["receipt"]["sha256"] = "0" * 64
    elif case == "no-prior":
        response = {"ok": True, "action": "no-prior-tls-generation"}
    calls: list[list[str]] = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        assert arguments == [str(paths.runtime / "bin/termdeck-web-prepare"), "--restore-unreceipted-tls-generation"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(response))

    monkeypatch.setattr(activation, "_run", run)
    checkpoints: list[str] = []

    class ResultCheckpointCrash(BaseException):
        pass

    def checkpoint(record: str):
        checkpoints.append(record)
        if case == "crash-before-result" and record == "rollback:generated-runtime":
            # Model process death after the producer restored A but before
            # the outer result was durable: reload only the prior intent.
            raise ResultCheckpointCrash()
        activation._write(tx / "receipt.json", receipt)

    if case in {"missing-field", "wrong-output", "wrong-receipt", "no-prior"}:
        with pytest.raises(activation.WebActivationError, match="inverse|prior generation"):
            activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=checkpoint)
        assert "runtime_rollback" not in preimages["generated:build-receipt.json"]
        assert all("rollback_postimage" not in record for record in preimages.values())
        return

    if case == "crash-before-result":
        with pytest.raises(ResultCheckpointCrash):
            activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=checkpoint)
        receipt = activation._read(tx / "receipt.json")
        preimages = receipt["preimages"]
        assert "runtime_rollback_intent" in preimages["generated:build-receipt.json"]
        assert "runtime_rollback" not in preimages["generated:build-receipt.json"]
        checkpoint = checkpoints.append

    assert activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=checkpoint) is True
    assert checkpoints[-1] == "rollback:generated-runtime"
    assert len(calls) == (2 if case == "crash-before-result" else 1)
    for record in preimages.values():
        assert record["rollback_restore"] == "restored"
        assert isinstance(record["rollback_postimage"], dict)
    monkeypatch.setattr(activation, "_run", lambda *_args, **_kwargs: pytest.fail("completed inverse reran producer"))
    if case == "foreign-replay":
        target = paths.runtime / "current-lan-ip"
        activation._atomic(target, target.read_bytes(), 0o600)
        foreign_inode = target.stat().st_ino
        with pytest.raises(activation.WebActivationError, match="restored claim|changed before rollback"):
            activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=checkpoint)
        assert target.stat().st_ino == foreign_inode
    else:
        assert activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=checkpoint) is True


@pytest.mark.parametrize("case", ("intact-a", "absent", "unexpected-tls", "foreign-a"))
def test_failed_start_skips_runtime_inverse_only_for_exact_unchanged_preimages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A failed start can leave A untouched; unexplained new leaves still need recovery."""

    paths = _paths(tmp_path)
    if case in {"intact-a", "foreign-a"}:
        _write_runtime_preparer_outputs(paths)
    tx, receipt = activation.new_transaction(paths, source={"commit": "c" * 40})
    preimages = _generated_runtime_preimages(paths, tx)
    receipt.update(preimages=preimages, phase_history=["services-starting"])
    if case == "unexpected-tls":
        (paths.runtime / "tls").mkdir(mode=0o700)
    elif case == "foreign-a":
        current = paths.runtime / "current-lan-ip"
        activation._atomic(current, current.read_bytes(), 0o600)
    calls: list[list[str]] = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        assert case in {"unexpected-tls", "foreign-a"}
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"action":"no-prior-tls-generation"}')

    monkeypatch.setattr(activation, "_run", run)
    activation._atomic(paths.runtime / "bin/termdeck-web-prepare", b"#!/bin/sh\n", 0o755)
    if case in {"unexpected-tls", "foreign-a"}:
        with pytest.raises(activation.WebActivationError, match="no authenticated prior"):
            activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=lambda _record: None)
        assert len(calls) == 1
    else:
        assert activation._restore_tls_generation(paths, tx, receipt, preimages, checkpoint=lambda _record: None) is False
        assert calls == []
    assert all("rollback_postimage" not in record for record in preimages.values())


def test_generated_runtime_refuses_copied_v2_receipt_from_another_runtime(
    tmp_path: Path,
) -> None:
    """A byte-identical copied receipt has neither its own inode nor sources."""

    source_paths = _paths(tmp_path / "source-publication")
    _write_runtime_preparer_outputs(source_paths)

    paths = _paths(tmp_path / "copied-publication")
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages = _generated_runtime_preimages(paths, tx)
    copied = (
        *sorted(activation.RUNTIME_PREPARER_SOURCE_FILES),
        *activation.GENERATED_RUNTIME,
    )
    for relative in copied:
        activation._atomic(
            paths.runtime / relative,
            (source_paths.runtime / relative).read_bytes(),
            0o644 if relative == "build-receipt.json" else 0o600,
        )
    # Use the same trusted public key so rejection proves that a valid
    # signature cannot authorize a different receipt path or publication.
    target_config = _prepare_test_attestor(paths)
    activation._atomic(
        target_config.public_key_path,
        _test_attestation_config(source_paths).public_key_path.read_bytes(),
        runtime_attestation.PUBLIC_KEY_MODE,
    )

    with pytest.raises(activation.WebActivationError, match="path|identity"):
        activation._capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=lambda _record: pytest.fail("copied runtime receipt was adopted"),
        )
    assert all(record["postimage"] is None for record in preimages.values())


def test_generated_runtime_refuses_republished_v2_receipt_leaf(
    tmp_path: Path,
) -> None:
    """Even an exact receipt copy loses its producer-bound leaf identity."""

    paths = _paths(tmp_path)
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages = _generated_runtime_preimages(paths, tx)
    _write_runtime_preparer_outputs(paths)
    receipt_path = paths.runtime / "build-receipt.json"
    copied_payload = receipt_path.read_bytes()
    activation._atomic(receipt_path, copied_payload, 0o644)

    with pytest.raises(activation.WebActivationError, match="receipt identity"):
        activation._capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=lambda _record: pytest.fail("republished receipt was adopted"),
        )
    assert all(record["postimage"] is None for record in preimages.values())


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("lan_ip", "8.8.8.8", "LAN address"),
        ("generation", "A" * 32, "runtime receipt publication"),
        ("source_digest", "A" * 64, "runtime receipt file"),
    ],
)
def test_generated_runtime_v2_rejects_noncanonical_bound_fields(
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    """The strict v2 parser shares the producer's canonical field contract."""

    paths = _paths(tmp_path)
    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    preimages = _generated_runtime_preimages(paths, tx)
    _write_runtime_preparer_outputs(paths)
    receipt_path = paths.runtime / "build-receipt.json"
    parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
    if field == "generation":
        parsed["runtime_publication"]["generation"] = value
    elif field == "source_digest":
        parsed["files"]["bin/termdeck-web-server"] = value
    else:
        parsed[field] = value
    _rewrite_regular_same_inode(
        receipt_path,
        (json.dumps(parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )

    with pytest.raises(activation.WebActivationError, match=error):
        activation._capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=lambda _record: pytest.fail("noncanonical runtime receipt was adopted"),
        )


@pytest.mark.parametrize("failure_phase", ("start", "health-and-inverse-reload"))
def test_start_failure_after_lan_preparation_adopts_generated_outputs_before_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    from termdeck import legacy_activation
    monkeypatch.setattr(legacy_activation, "prepare_recovery", lambda *_args: None)
    """A start failure before normal capture still leaves generated B rollback-owned."""

    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    runtime_config = paths.runtime / "bin/checkpoint.conf"
    activation._atomic(runtime_config, b"runtime-a", 0o600)
    old_release_id = "a" * 64
    new_release_id = "b" * 64
    activation._write(
        paths.managed_root / "state.json",
        {"current_release_id": old_release_id},
    )
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": "qualified.whl",
                "manifest": "qualified.release.json",
                "release_id": new_release_id,
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)
    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    staged_config = tx / "staged-runtime/bin/checkpoint.conf"
    staged_config.parent.mkdir(parents=True)
    staged_config.write_bytes(b"runtime-b")
    monkeypatch.setattr(
        activation, "_sources", lambda *_args: {"bin/checkpoint.conf": (staged_config, 0o600)}
    )
    def arm_watchdog(_paths, _tx, value, *_args, **_kwargs):
        value["watchdog"] = {
            "unit": activation.WATCHDOG_UNIT, "armed_before_disruption": True
        }

    monkeypatch.setattr(activation, "_arm_and_record_watchdog", arm_watchdog)
    monkeypatch.setattr(activation, "_publish_units", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        activation,
        "_install",
        lambda *_args, **_kwargs: (
            activation._write(
                paths.managed_root / "state.json",
                {"current_release_id": new_release_id},
            )
            or {"ok": True, "release_id": new_release_id}
        ),
    )
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_assert_package_rollback_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation,
        "_publish_wrappers",
        lambda _paths, release_id: {"ok": True, "release_id": release_id},
    )
    monkeypatch.setattr(
        activation,
        "_capture_wrapper_postimages",
        lambda *_args, **_kwargs: {"ok": True, "release_id": new_release_id},
    )
    service_events: list[str] = []
    monkeypatch.setattr(activation, "_stop_web", lambda: service_events.append("stop"))
    monkeypatch.setattr(activation, "_disarm", lambda: service_events.append("disarm"))
    # This fixture isolates capture/outer restore ordering.  Dedicated TLS
    # inverse coverage exercises the preparer's real command contract.
    def restore_tls_after_sources(*_args, **_kwargs):
        assert runtime_config.read_bytes() == b"runtime-a"
        return False

    monkeypatch.setattr(activation, "_restore_tls_generation", restore_tls_after_sources)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda *_args: {"integrity": "ok", "lost_session_ids": [], "preserved_session_ids": 0},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda *_args: {"disappeared": [], "natural_exit_allowed": True},
    )
    starts = 0

    def start_web(_paths) -> None:
        nonlocal starts
        starts += 1
        if starts != 1:
            return
        _write_runtime_preparer_outputs(paths, generation="b" * 32)
        if failure_phase == "start":
            raise RuntimeError("LAN start failed after runtime preparation")

    def rollback_package(_paths: activation.ActivationPaths, release: object, **_kwargs) -> dict[str, object]:
        assert release == old_release_id
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": old_release_id},
        )
        return {"ok": True}

    monkeypatch.setattr(activation, "_start_web", start_web)
    monkeypatch.setattr(activation, "_rollback_package", rollback_package)

    inverse_reload_failed = False
    if failure_phase == "health-and-inverse-reload":
        def systemd(*args: str, **kwargs):
            nonlocal inverse_reload_failed
            durable = activation._read(tx / "receipt.json")
            runtime_record = durable.get("preimages", {}).get("runtime:bin/checkpoint.conf", {})
            if args[0] == "daemon-reload" and runtime_record.get("rollback_postimage") and not inverse_reload_failed:
                inverse_reload_failed = True
                raise RuntimeError("inverse daemon reload failed once")
            return SimpleNamespace(returncode=0)

        def health():
            raise RuntimeError("LAN health failed after runtime preparation")

        monkeypatch.setattr(activation, "_systemd", systemd)
        monkeypatch.setattr(activation, "_health", health)

    with pytest.raises(RuntimeError, match="LAN (start|health) failed after runtime preparation"):
        activation.execute(paths, tx)

    if failure_phase == "health-and-inverse-reload":
        failed = activation._read(tx / "receipt.json")
        assert inverse_reload_failed
        assert failed["status"] == "rollback-failed"
        assert failed["services_may_be_live"] is False
        assert failed["watchdog"]["disarmed_for_rollback"] is True
        assert failed["preimages"]["runtime:bin/checkpoint.conf"]["rollback_postimage"]
        assert runtime_config.read_bytes() == b"runtime-a"
        assert failed["rollback_error"] == "inverse daemon reload failed once"
        completed_service_events = list(service_events)
        assert activation.rollback(paths, tx, reason="retry inverse reload failure")["action"] == "rolled-back"
        assert service_events == completed_service_events

    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    preimages = final["preimages"]
    assert isinstance(preimages, dict)
    for relative in activation.GENERATED_RUNTIME:
        record = preimages["generated:" + relative]
        assert isinstance(record, dict)
        assert isinstance(record["postimage"], dict)
        assert record["rollback_restore"] == ("already-restored" if failure_phase == "health-and-inverse-reload" else "restored")
        assert not os.path.lexists(paths.runtime / relative)
    assert runtime_config.read_bytes() == b"runtime-a"
    assert not paths.recovery_required.exists()


@pytest.mark.parametrize(
    ("crash_record", "old_release_id"),
    (
        ("unit:" + activation.WATCHDOG_UNIT, "a" * 64),
        ("runtime:bin/demo", "a" * 64),
        ("unit:" + activation.SERVICES[0], "a" * 64),
        ("unit:" + activation.WATCHDOG_UNIT, None),
    ),
    ids=("watchdog-a-before-b", "runtime-b", "service-unit-b", "first-install-watchdog-b"),
)
def test_published_postimage_is_durable_before_simulated_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_record: str,
    old_release_id: str | None,
) -> None:
    from termdeck import legacy_activation
    monkeypatch.setattr(legacy_activation, "prepare_recovery", lambda *_args: None)
    """Every published leaf is durable enough for a new process to roll back."""

    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    new_release_id = "b" * 64
    recovery_wrapper = paths.managed_root / "termdeck-web-recovery"
    old_wrapper = b"#!/bin/sh\nexec true\n"
    new_wrapper = b"#!/bin/sh\nexec false\n"
    # This fixture isolates the outer publication checkpoint from the release
    # manager's full wrapper renderer.  The one simulated wrapper is still
    # captured through the production outer postimage path.
    monkeypatch.setattr(
        activation,
        "_wrapper_publication_paths",
        lambda _paths: (recovery_wrapper,),
    )
    if old_release_id is not None:
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": old_release_id, "generation": 1},
        )
        activation._atomic(recovery_wrapper, old_wrapper, 0o755)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {
                "wheel": "qualified.whl",
                "manifest": "qualified.release.json",
                "release_id": new_release_id,
            },
        }
    )
    activation._write(tx / "receipt.json", receipt)
    staged_runtime = tx / "staged-runtime" / "bin"
    staged_runtime.mkdir(parents=True)
    (staged_runtime / "demo").write_bytes(b"runtime-b")
    staged_units = tx / "staged-units"
    staged_units.mkdir()
    for unit in activation.SERVICES:
        (staged_units / unit).write_text(f"[Unit]\nDescription={unit}\n", encoding="utf-8")

    class SimulatedCrash(BaseException):
        pass

    class NoHeartbeatThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return False

        def join(self) -> None:
            return None

    watchdog_state = {"active": True, "enabled": True}

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        if args[0] == "is-active":
            return SimpleNamespace(
                returncode=0 if watchdog_state["active"] else 3,
                stdout="active\n" if watchdog_state["active"] else "inactive\n",
            )
        if args[0] == "is-enabled":
            return SimpleNamespace(
                returncode=0 if watchdog_state["enabled"] else 1,
                stdout="enabled\n" if watchdog_state["enabled"] else "disabled\n",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation.threading, "Thread", NoHeartbeatThread)
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(
        activation,
        "_sources",
        lambda _paths, _tx: {"bin/demo": (tx / "staged-runtime" / "bin" / "demo", 0o755)},
    )
    monkeypatch.setattr(activation, "_systemd", systemd)
    def install_b(_paths: activation.ActivationPaths, _package: Mapping[str, object], **_kwargs) -> dict[str, object]:
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": new_release_id, "generation": 2},
        )
        activation._atomic(recovery_wrapper, new_wrapper, 0o755)
        return {"ok": True, "release_id": new_release_id}

    monkeypatch.setattr(activation, "_install", install_b)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation,
        "_publish_wrappers",
        lambda _paths, release_id: {"ok": True, "release_id": release_id},
    )
    monkeypatch.setattr(
        activation,
        "_preflight_watchdog_wrapper",
        lambda _paths, release_id: {
            "ok": True,
            "release_id": release_id,
            "manifest_sha256": "c" * 64,
            **_fixture_wrapper_execution_identity(release_id),
        },
    )
    monkeypatch.setattr(
        activation,
        "_wrapper_execution_identity",
        lambda _paths, release_id: _fixture_wrapper_execution_identity(release_id),
    )
    monkeypatch.setattr(
        activation,
        "_watchdog_manifest_postimage",
        lambda *_args, **_kwargs: {"fixture": "manifest"},
    )
    monkeypatch.setattr(activation, "_assert_current_postimage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(activation, "_stop_web", lambda: None)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda *_args: {"integrity": "ok", "lost_session_ids": [], "preserved_session_ids": 0},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda *_args: {"disappeared": [], "natural_exit_allowed": True},
    )
    real_checkpoint = activation._checkpoint_publication

    def crash_after_durable_checkpoint(paths, transaction, value, record):
        real_checkpoint(paths, transaction, value, record)
        if record == crash_record:
            raise SimulatedCrash(record)

    monkeypatch.setattr(activation, "_checkpoint_publication", crash_after_durable_checkpoint)

    with pytest.raises(SimulatedCrash, match=crash_record):
        activation.execute(paths, tx)

    durable = activation._read(tx / "receipt.json")
    durable_preimages = durable["preimages"]
    assert isinstance(durable_preimages, dict)
    published = durable_preimages[crash_record]
    assert isinstance(published, dict)
    assert isinstance(published["postimage"], dict)
    journal = durable["publication_journal"]
    assert isinstance(journal, list)
    journal_records = [entry["record"] for entry in journal]
    prepackage_watchdog_crash = (
        crash_record == "unit:" + activation.WATCHDOG_UNIT
        and old_release_id is not None
    )
    if prepackage_watchdog_crash:
        assert "package:managed-release" not in journal_records
        assert activation._managed_release_id(paths) == old_release_id
    else:
        assert "package:managed-release" in journal_records
        assert activation._managed_release_id(paths) == new_release_id
    assert crash_record in journal_records
    assert durable["old_release_id"] == old_release_id

    # Resume as a new process after the deliberately uncaught crash.  The
    # terminal rollback must restore the exact preimage rather than marking
    # recovery required merely because B was already made visible.
    watchdog_state.update(active=False, enabled=False)

    package_rollbacks: list[object] = []

    def rollback_package(_paths: activation.ActivationPaths, release: object, **_kwargs) -> dict[str, object]:
        assert release == old_release_id
        package_rollbacks.append(release)
        state = paths.managed_root / "state.json"
        if release is None:
            state.unlink()
            recovery_wrapper.unlink()
        else:
            activation._write(state, {"current_release_id": release, "generation": 1})
            # The outer receipt owns this B wrapper postimage and restores A
            # only after the package API has authenticated B and selected A.
            # Leaving it in place models that ordering without forging a
            # fresh same-content A inode in the fixture.
        return {"ok": True}

    monkeypatch.setattr(activation, "_rollback_package", rollback_package)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_assert_package_rollback_owned", lambda *_args, **_kwargs: None)
    result = activation.rollback(paths, tx, reason="simulated process crash")

    assert result["action"] == "rolled-back"
    assert activation._managed_release_id(paths) == old_release_id
    assert package_rollbacks == ([] if prepackage_watchdog_crash else [old_release_id])
    target = activation._record_target(published)
    assert not os.path.lexists(target)
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    assert not paths.recovery_required.exists()


def test_a_to_b_crash_after_armed_receipt_before_install_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from termdeck import legacy_activation
    monkeypatch.setattr(legacy_activation, "prepare_recovery", lambda *_args: None)
    """A's verified watchdog survives the pre-B package crash boundary."""

    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    release_a = "a" * 64
    release_b = "b" * 64
    recovery_wrapper = paths.managed_root / "termdeck-web-recovery"
    activation._write(
        paths.managed_root / "state.json",
        {"current_release_id": release_a, "generation": 7},
    )
    activation._atomic(recovery_wrapper, b"#!/bin/sh\nexit 0\n", 0o755)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {"wheel": "qualified.whl", "manifest": "qualified.release.json", "release_id": release_b},
        }
    )
    activation._write(tx / "receipt.json", receipt)

    class SimulatedCrash(BaseException):
        pass

    class NoHeartbeatThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self) -> None:
            return None

    watchdog_state = {"active": False, "enabled": False}
    systemd_calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        systemd_calls.append(args)
        if args[0] == "enable":
            watchdog_state.update(active=True, enabled=True)
        elif args[0] == "disable":
            watchdog_state.update(active=False, enabled=False)
        elif args[0] == "is-active":
            return SimpleNamespace(
                returncode=0 if watchdog_state["active"] else 3,
                stdout="active\n" if watchdog_state["active"] else "inactive\n",
            )
        elif args[0] == "is-enabled":
            return SimpleNamespace(
                returncode=0 if watchdog_state["enabled"] else 1,
                stdout="enabled\n" if watchdog_state["enabled"] else "disabled\n",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation.threading, "Thread", NoHeartbeatThread)
    monkeypatch.setattr(activation, "snapshot_database", lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"})
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda *_args: {})
    monkeypatch.setattr(activation, "_systemd", systemd)
    monkeypatch.setattr(
        activation,
        "_preflight_watchdog_wrapper",
        lambda _paths, release_id: {
            "ok": True,
            "release_id": release_id,
            "manifest_sha256": "e" * 64,
            **_fixture_wrapper_execution_identity(release_id),
        },
    )
    monkeypatch.setattr(
        activation,
        "_wrapper_execution_identity",
        lambda _paths, release_id: _fixture_wrapper_execution_identity(release_id),
    )
    monkeypatch.setattr(
        activation,
        "_watchdog_manifest_postimage",
        lambda *_args, **_kwargs: {"fixture": "manifest"},
    )
    monkeypatch.setattr(activation, "_assert_current_postimage", lambda *_args, **_kwargs: None)
    install_attempts: list[object] = []

    def install_must_not_run(*_args, **_kwargs):
        install_attempts.append("B")
        pytest.fail("B install ran after the armed-receipt crash point")

    monkeypatch.setattr(activation, "_install", install_must_not_run)
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"integrity": "ok", "lost_session_ids": []})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"disappeared": [], "natural_exit_allowed": True})
    real_persist = activation._persist

    def crash_after_armed(paths, transaction, value, status=None):
        result = real_persist(paths, transaction, value, status)
        if status == "watchdog-armed":
            raise SimulatedCrash("after A watchdog arm before B install")
        return result

    monkeypatch.setattr(activation, "_persist", crash_after_armed)

    with pytest.raises(SimulatedCrash, match="before B install"):
        activation.execute(paths, tx)

    durable = activation._read(tx / "receipt.json")
    watchdog = durable["watchdog"]
    assert isinstance(watchdog, dict)
    assert durable["status"] == "watchdog-armed"
    assert watchdog["armed_identity"]["release_id"] == release_a
    assert watchdog["armed_identity"]["state_generation"] == 7
    assert activation._managed_release_id(paths) == release_a
    assert install_attempts == []
    assert "package:managed-release" not in [entry["record"] for entry in durable["publication_journal"]]

    result = activation.rollback(paths, tx, reason="crash after A watchdog arm")

    assert result["action"] == "rolled-back"
    assert activation._managed_release_id(paths) == release_a
    assert ("disable", "--now", activation.WATCHDOG_UNIT) in systemd_calls
    assert not paths.recovery_required.exists()


def test_first_install_crash_after_wrapper_journal_before_watchdog_arm_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable first-install wrapper checkpoint can be retried without a watchdog."""

    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    release_b = "b" * 64
    recovery_wrapper = paths.managed_root / "termdeck-web-recovery"
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "status": "artifacts-staged",
            "package": {"wheel": "qualified.whl", "manifest": "qualified.release.json", "release_id": release_b},
        }
    )
    activation._write(tx / "receipt.json", receipt)

    class SimulatedCrash(BaseException):
        pass

    class NoHeartbeatThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self) -> None:
            return None

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation.threading, "Thread", NoHeartbeatThread)
    monkeypatch.setattr(activation, "snapshot_database", lambda *_args: {"session_ids": [], "session_count": 0, "integrity": "ok"})
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda *_args: {})
    monkeypatch.setattr(activation, "_systemd", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))

    def install_b(*_args, **_kwargs) -> dict[str, object]:
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": release_b, "generation": 1},
        )
        activation._atomic(recovery_wrapper, b"#!/bin/sh\nexit 0\n", 0o755)
        return {"ok": True, "release_id": release_b}

    monkeypatch.setattr(activation, "_install", install_b)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation,
        "_publish_wrappers",
        lambda _paths, release_id: {"ok": True, "release_id": release_id},
    )
    monkeypatch.setattr(
        activation,
        "_capture_wrapper_postimages",
        lambda *_args, **_kwargs: {"ok": True, "release_id": release_b},
    )
    monkeypatch.setattr(
        activation,
        "_arm_and_record_watchdog",
        lambda *_args, **_kwargs: pytest.fail("watchdog arm ran after wrapper-journal crash point"),
    )
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"integrity": "ok", "lost_session_ids": []})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"disappeared": [], "natural_exit_allowed": True})
    real_checkpoint = activation._checkpoint_publication

    def crash_after_wrapper_journal(paths, transaction, value, record):
        real_checkpoint(paths, transaction, value, record)
        if record == "wrapper:managed-release":
            raise SimulatedCrash("after first-install wrapper journal before watchdog arm")

    monkeypatch.setattr(activation, "_checkpoint_publication", crash_after_wrapper_journal)

    with pytest.raises(SimulatedCrash, match="wrapper journal before watchdog"):
        activation.execute(paths, tx)

    durable = activation._read(tx / "receipt.json")
    assert durable["old_release_id"] is None
    assert "watchdog" not in durable
    assert activation._managed_release_id(paths) == release_b
    assert [entry["record"] for entry in durable["publication_journal"]] == [
        "package:managed-release",
        "wrapper:managed-release",
    ]

    package_rollbacks: list[object] = []

    def rollback_b(_paths: activation.ActivationPaths, release: object, **_kwargs) -> dict[str, object]:
        package_rollbacks.append(release)
        (paths.managed_root / "state.json").unlink()
        recovery_wrapper.unlink()
        return {"ok": True}

    monkeypatch.setattr(activation, "_rollback_package", rollback_b)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_assert_package_rollback_owned", lambda *_args, **_kwargs: None)
    result = activation.rollback(paths, tx, reason="resume first-install wrapper-journal crash")

    assert result["action"] == "rolled-back"
    assert package_rollbacks == [None]
    assert activation._managed_release_id(paths) is None
    assert not os.path.lexists(recovery_wrapper)
    assert not paths.recovery_required.exists()


def test_rollback_stops_live_b_and_disarms_before_package_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B's web processes and recovery unit stop before B identity is removed."""

    paths = _paths(tmp_path)
    paths.managed_root.mkdir()
    release_a = "a" * 64
    release_b = "b" * 64
    activation._write(paths.managed_root / "state.json", {"current_release_id": release_b, "generation": 2})
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt.update(
        {
            "mutation_started": True,
            "old_release_id": release_a,
            "preimages": {},
            "database_before": {"session_ids": []},
            "processes_before": [],
            "services_may_be_live": True,
            "watchdog": {"unit": activation.WATCHDOG_UNIT, "armed_before_disruption": True},
        }
    )
    # A crash may occur while systemd is stopping B and after a package journal
    # independently returned state.json to A.  The current true marker keeps
    # the stop requirement durable before a later package inverse can run.
    activation._persist(paths, tx, receipt, "services-stopping")
    activation._persist(paths, tx, receipt, "failed")
    events: list[str] = []
    monkeypatch.setattr(activation, "_stop_web", lambda: events.append("stop-b-web"))
    monkeypatch.setattr(activation, "_disarm", lambda: events.append("disarm-watchdog"))

    def rollback_package(_paths: activation.ActivationPaths, release: object, **_kwargs) -> dict[str, object]:
        assert release == release_a
        assert events == ["stop-b-web", "disarm-watchdog"]
        events.append("rollback-package")
        activation._write(paths.managed_root / "state.json", {"current_release_id": release_a, "generation": 1})
        return {"ok": True}

    monkeypatch.setattr(activation, "_rollback_package", rollback_package)
    monkeypatch.setattr(activation, "_assert_release_publication_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_assert_package_rollback_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_systemd", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(activation, "_start_web", lambda _paths: events.append("start-a-web"))
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"integrity": "ok", "lost_session_ids": []})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"disappeared": [], "natural_exit_allowed": True})

    result = activation.rollback(paths, tx, reason="B process may still be live")

    assert result["action"] == "rolled-back"
    assert events[:3] == ["stop-b-web", "disarm-watchdog", "rollback-package"]
    final = activation._read(tx / "receipt.json")
    assert final["services_stopped"] is True
    assert final["services_may_be_live"] is False
    assert final["watchdog"]["disarmed_for_rollback"] is True


def test_explicit_services_not_live_marker_overrides_history_and_skips_absent_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-install retry does not stop units its inverse already removed."""

    receipt = {
        "status": "services-starting",
        "phase_history": ["services-stopping", "services-starting"],
        "services_may_be_live": False,
    }
    assert activation._b_services_may_be_live(receipt) is False

    calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        if args[0] == "stop":
            pytest.fail("attempted to stop a unit already restored absent")
        assert args[0] == "show"
        return SimpleNamespace(
            returncode=3,
            stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\n",
        )

    monkeypatch.setattr(activation, "_systemd", systemd)
    activation._stop_web()
    assert calls
    assert all(call[0] == "show" for call in calls)


def test_stop_web_rejects_timed_out_execstop_even_when_unit_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        if args[0] == "show":
            state = "failed" if any(call[0] == "stop" for call in calls) else "active"
            return SimpleNamespace(returncode=0, stdout=f"LoadState=loaded\nActiveState={state}\n")
        assert args[0] == "stop" and args[1] == "termdeck-web-lan.service"
        return SimpleNamespace(returncode=1, stdout="", stderr="stop timed out")

    monkeypatch.setattr(activation, "_systemd", systemd)
    with pytest.raises(activation.WebActivationError, match="could not stop: termdeck-web-lan.service"):
        activation._stop_web()
    assert not any(args[:2] == ("stop", "termdeck-web-backend.service") for args in calls)


@pytest.mark.parametrize("recovery_marker", ("owned", "foreign", "symlink"))
def test_rollback_retry_recognizes_an_already_restored_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_marker: str,
) -> None:
    """A crash after restoring A cannot turn a safe retry into recovery-required."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    target = paths.runtime / "rollback-retry.conf"
    activation._atomic(target, b"preimage-a", 0o600)
    record = activation._preimage(target, tx, "rollback-retry.conf")
    activation._publish_preimaged(
        record,
        tx,
        b"postimage-b",
        0o600,
        label="rollback retry fixture",
    )
    receipt.update(
        {
            "mutation_started": True,
            "preimages": {"runtime:rollback-retry.conf": record},
            "old_release_id": None,
            "database_before": {"session_ids": []},
            "processes_before": [],
            "watchdog": {"unit": activation.WATCHDOG_UNIT, "armed_before_disruption": True},
        }
    )
    activation._persist(paths, tx, receipt, "failed")
    marker_content = (str(tx) + "\n").encode()
    foreign_marker = tmp_path / "foreign-recovery-marker"
    if recovery_marker == "symlink":
        foreign_marker.write_bytes(marker_content)
        paths.recovery_required.symlink_to(foreign_marker)
    else:
        activation._atomic(
            paths.recovery_required,
            marker_content if recovery_marker == "owned" else b"foreign transaction\n",
            0o600,
        )

    # This is the point at which a process can die after the leaf exchange but
    # before it records the ordinary ``rollback_restore`` progress marker.  The
    # exact restored-A postimage itself is already durable, so a new process
    # can distinguish it from a same-byte foreign replacement.
    assert activation._restore(
        record,
        tx,
        checkpoint=lambda: activation._checkpoint_publication(
            paths,
            tx,
            receipt,
            "rollback:runtime:rollback-retry.conf",
        ),
    ) == "restored"
    assert target.read_bytes() == b"preimage-a"

    events: list[str] = []
    real_persist = activation._persist

    def tracked_persist(paths, transaction, value, status=None):
        if status == "rolled-back":
            assert events == ["disarm"]
        return real_persist(paths, transaction, value, status)

    monkeypatch.setattr(activation, "_persist", tracked_persist)
    monkeypatch.setattr(activation, "_systemd", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(activation, "_start_web", lambda _paths: None)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda *_args: {"integrity": "ok", "lost_session_ids": []},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda *_args: {"disappeared": [], "natural_exit_allowed": True},
    )
    monkeypatch.setattr(activation, "_disarm", lambda: events.append("disarm"))

    result = activation.rollback(paths, tx, reason="crashed after restoring A")

    assert result["action"] == "rolled-back"
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rolled-back"
    final_preimages = final["preimages"]
    assert isinstance(final_preimages, dict)
    final_record = final_preimages["runtime:rollback-retry.conf"]
    assert isinstance(final_record, dict)
    assert final_record["rollback_restore"] == "already-restored"
    assert target.read_bytes() == b"preimage-a"
    if recovery_marker == "owned":
        assert not os.path.lexists(paths.recovery_required)
    elif recovery_marker == "foreign":
        assert paths.recovery_required.read_bytes() == b"foreign transaction\n"
    else:
        assert paths.recovery_required.is_symlink()
        assert foreign_marker.read_bytes() == marker_content


def test_restore_retry_rejects_a_same_content_foreign_leaf_after_preimage_returned(
    tmp_path: Path,
) -> None:
    """Idempotency accepts the exact restored preimage, never a third value."""

    target = tmp_path / "runtime" / "rollback-retry.conf"
    tx = tmp_path / "transaction"
    tx.mkdir()
    activation._atomic(target, b"preimage-a", 0o600)
    record = activation._preimage(target, tx, "rollback-retry.conf")
    activation._publish_preimaged(
        record,
        tx,
        b"postimage-b",
        0o600,
        label="foreign retry fixture",
    )
    assert activation._restore(record, tx) == "restored"
    # A fresh inode with A's exact bytes is still a foreign replacement.  The
    # rollback retry must use the receipt-owned restored identity, not content
    # equality, to decide that an earlier inverse completed.
    activation._atomic(target, b"preimage-a", 0o600)

    with pytest.raises(activation.WebActivationError, match="changed before rollback"):
        activation._restore(record, tx)

    assert target.read_bytes() == b"preimage-a"


@pytest.mark.parametrize("prior_exists", (True, False), ids=("existing-a", "first-install"))
@pytest.mark.parametrize("crash_phase", ("intent", "published"))
def test_restore_hard_death_recovers_distinct_inverse_journal(
    tmp_path: Path, prior_exists: bool, crash_phase: str
) -> None:
    """A fresh process recovers the inverse's exact B before retrying A/absence."""

    target = tmp_path / "runtime/config"
    tx = tmp_path / "transaction"
    tx.mkdir()
    if prior_exists:
        activation._atomic(target, b"preimage-a", 0o600)
    record = activation._preimage(target, tx, "config")
    activation._publish_preimaged(
        record, tx, b"postimage-b", 0o600, label="inverse crash fixture"
    )
    record_path = tx / "outer-record.json"
    activation._write(record_path, record)
    child = r'''
import os, sys
from pathlib import Path
from termdeck import web_activation as activation
tx, phase = Path(sys.argv[1]), sys.argv[2]
record_path = tx / "outer-record.json"
record = activation._read(record_path)
def checkpoint():
    activation._write(record_path, record)
    if record.get("rollback_publication", {}).get("state") == phase:
        os._exit(89)
activation._restore(record, tx, checkpoint=checkpoint)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(tx), crash_phase],
        cwd=Path(__file__).parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert crashed.returncode == 89, crashed.stderr
    record = activation._read(record_path)
    inverse = record["rollback_publication"]
    inverse_journal = Path(inverse["receipt"]["journal_path"])
    assert inverse["state"] == crash_phase
    assert inverse_journal.exists()
    assert inverse_journal != activation._publication_journal_path(target, tx)
    recovered_b: list[dict[str, object]] = []

    def checkpoint_recovery():
        activation._write(record_path, record)
        if record["rollback_publication"]["state"] == "recovered":
            assert target.read_bytes() == b"postimage-b"
            assert record["postimage"]["identity"]["inode"] == target.stat().st_ino
            recovered_b.append(record["postimage"])

    assert activation._restore(record, tx, checkpoint=checkpoint_recovery) == "restored"
    assert recovered_b
    assert not inverse_journal.exists()
    assert _file_preimage(target) == ((b"preimage-a", 0o600) if prior_exists else None)
    assert activation._restore(record, tx) == "already-restored"


@pytest.mark.parametrize("inactive_state", ("inactive", "failed"))
def test_disarm_requires_disable_reload_and_inactive_disabled_results(
    monkeypatch: pytest.MonkeyPatch,
    inactive_state: str,
) -> None:
    calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        outcomes = {
            "disable": 0,
            "daemon-reload": 0,
            "is-active": 3,
            "is-enabled": 1,
        }
        return SimpleNamespace(
            returncode=outcomes[args[0]],
            stdout={"is-active": inactive_state + "\n", "is-enabled": "disabled\n"}.get(args[0], ""),
        )

    monkeypatch.setattr(activation, "_systemd", systemd)
    activation._disarm()

    assert calls == [
        ("disable", "--now", activation.WATCHDOG_UNIT),
        ("daemon-reload",),
        ("is-active", activation.WATCHDOG_UNIT),
        ("is-enabled", activation.WATCHDOG_UNIT),
    ]


def test_watchdog_self_disarm_survives_until_terminal_rollback_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery unit must finish its inverse before its own process exits."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt.update({
        "status": "services-starting",
        "mutation_started": True,
        "heartbeat_epoch": time.time() - activation.WATCHDOG_TIMEOUT - 1,
        "services_may_be_live": True,
        "old_release_id": None,
        "preimages": {},
        "watchdog": {"unit": activation.WATCHDOG_UNIT, "armed_before_disruption": True},
    })
    activation._write(tx / "receipt.json", receipt)
    events: list[object] = []

    def systemd(*args: str, check: bool = True):
        events.append(args)
        assert "--now" not in args, "watchdog attempted to stop its own recovery process"
        assert args[0] != "is-active", "active recovery worker must survive until it returns"
        return SimpleNamespace(
            returncode=1 if args[0] == "is-enabled" else 0,
            stdout="disabled\n" if args[0] == "is-enabled" else "",
        )

    persist = activation._persist

    def checkpoint(paths, tx, value, status=None):
        if status == "rolled-back":
            assert ("disable", activation.WATCHDOG_UNIT) in events
            assert ("is-enabled", activation.WATCHDOG_UNIT) in events
            events.append("terminal-checkpoint")
        return persist(paths, tx, value, status)

    monkeypatch.setattr(activation, "_systemd", systemd)
    monkeypatch.setattr(activation, "_persist", checkpoint)
    monkeypatch.setattr(activation, "_stop_web", lambda: events.append("stop-b"))
    monkeypatch.setattr(activation, "_watchdog_owns_current_armed_identity", lambda *_args: True)
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"integrity": "ok"})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"disappeared": []})

    assert activation.watchdog(paths, tx) == 0
    assert events[-1] == "terminal-checkpoint"
    assert activation._read(tx / "receipt.json")["status"] == "rolled-back"
    assert not getattr(activation._WATCHDOG_EXECUTION, "recovering", False)


@pytest.mark.parametrize(
    ("outcomes", "message"),
    (
        ({"disable": 1}, "did not disable"),
        ({"daemon-reload": 1}, "daemon reload failed"),
        ({"is-active": 0}, "still active"),
        ({"is-enabled": 0}, "still enabled"),
    ),
    ids=("disable", "reload", "active", "enabled"),
)
def test_disarm_failure_refuses_to_claim_a_terminal_watchdog_state(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, int],
    message: str,
) -> None:
    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        defaults = {
            "disable": 0,
            "daemon-reload": 0,
            "is-active": 3,
            "is-enabled": 1,
        }
        return SimpleNamespace(
            returncode=outcomes.get(args[0], defaults[args[0]]),
            stdout={"is-active": "inactive\n", "is-enabled": "disabled\n"}.get(args[0], ""),
        )

    monkeypatch.setattr(activation, "_systemd", systemd)
    with pytest.raises(activation.WebActivationError, match=message):
        activation._disarm()


@pytest.mark.parametrize(
    ("query", "returncode", "stdout"),
    (
        ("is-active", 1, ""),
        ("is-active", 3, ""),
        ("is-active", 1, "inactive\n"),
        ("is-active", 4, "unknown\n"),
        ("is-active", 3, "active\n"),
        ("is-active", 3, "inactive\nactive\n"),
        ("is-enabled", 1, ""),
        ("is-enabled", 4, "not-found\n"),
        ("is-enabled", -15, "disabled\n"),
        ("is-enabled", 1, "enabled\n"),
        ("is-enabled", 1, "disabled\nenabled\n"),
    ),
)
def test_disarm_rejects_failed_or_ambiguous_state_queries(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    returncode: int,
    stdout: str,
) -> None:
    calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        if args[0] == query:
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="query unavailable")
        result = {
            "disable": (0, ""),
            "daemon-reload": (0, ""),
            "is-active": (3, "inactive\n"),
            "is-enabled": (1, "disabled\n"),
        }[args[0]]
        return SimpleNamespace(returncode=result[0], stdout=result[1], stderr="")

    monkeypatch.setattr(activation, "_systemd", systemd)
    with pytest.raises(activation.WebActivationError, match="could not verify recovery watchdog"):
        activation._disarm()
    assert calls[-1] == (query, activation.WATCHDOG_UNIT)


@pytest.mark.parametrize(
    ("query", "recovering_self"),
    (("is-active", False), ("is-enabled", False), ("is-enabled", True)),
)
def test_rollback_disarm_query_failure_leaves_a_recovery_actionable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    recovering_self: bool,
) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt.update(
        {
            "mutation_started": True,
            "old_release_id": None,
            "preimages": {},
            "database_before": {"session_ids": []},
            "processes_before": [],
            "watchdog": {"unit": activation.WATCHDOG_UNIT, "armed_before_disruption": True},
        }
    )
    activation._persist(paths, tx, receipt, "failed")
    calls: list[tuple[str, ...]] = []

    def systemd(*args: str, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        if args[0] == query:
            return SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus")
        result = {
            "disable": (0, ""),
            "daemon-reload": (0, ""),
            "is-active": (3, "inactive\n"),
        }[args[0]]
        return SimpleNamespace(returncode=result[0], stdout=result[1], stderr="")

    monkeypatch.setattr(activation, "_systemd", systemd)
    monkeypatch.setattr(activation._WATCHDOG_EXECUTION, "recovering", recovering_self, raising=False)
    monkeypatch.setattr(
        activation, "_recover_pending_release_publications",
        lambda *_args, **_kwargs: pytest.fail("package recovery ran after an unverified disarm"),
    )
    with pytest.raises(activation.WebActivationError, match="could not verify recovery watchdog"):
        activation.rollback(paths, tx, reason="watchdog disarm query failed")

    final = activation._read(tx / "receipt.json")
    assert final["status"] == "rollback-failed"
    assert paths.recovery_required.exists()
    assert final["watchdog"]["disarm_intent"] is True
    assert final["watchdog"].get("disarmed_for_rollback") is not True
    assert calls[0] == (("disable", activation.WATCHDOG_UNIT) if recovering_self else (
        "disable", "--now", activation.WATCHDOG_UNIT,
    ))
    assert calls[-1] == (query, activation.WATCHDOG_UNIT)
    if recovering_self:
        assert ("is-active", activation.WATCHDOG_UNIT) not in calls


def test_rolled_back_terminal_rollback_is_a_true_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated inverse cannot restart A or touch a newer service state."""

    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt.update({"mutation_started": True, "old_release_id": "a" * 64, "preimages": {}})
    activation._persist(paths, tx, receipt, "rolled-back")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("terminal rollback performed a new mutation")

    monkeypatch.setattr(activation, "_persist", unexpected)
    monkeypatch.setattr(activation, "_stop_web", unexpected)
    monkeypatch.setattr(activation, "_start_web", unexpected)
    monkeypatch.setattr(activation, "_disarm", unexpected)
    monkeypatch.setattr(activation, "_rollback_package", unexpected)

    result = activation.rollback(paths, tx, reason="repeat terminal rollback")

    assert result["action"] == "rolled-back"


@pytest.mark.parametrize("status", ("rolled-back", "rollback-failed"))
def test_terminal_transactions_load_and_watchdog_exit_cleanly(
    tmp_path: Path,
    status: str,
) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt.update({"status": status, "mutation_started": True})
    activation._write(tx / "receipt.json", receipt)

    loaded_tx, loaded = activation.load_transaction(paths, tx)
    assert loaded_tx == tx
    assert loaded["status"] == status
    assert activation.watchdog(paths, tx) == 0


def test_installed_recovery_command_restarts_without_mutating_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the qualified frozen watchdog twice, with no host service bus."""
    paths = _integration_paths(tmp_path)
    paths = dataclasses.replace(paths, evidence=paths.database.parent / "recovery" / "activation-v2")
    wheel, manifest, release = _release_artifact(tmp_path, "a")
    monkeypatch.setattr(
        release_manager, "_require_managed_storage", lambda _layout: _storage_attestation(),
    )
    release_manager.install_release(
        wheel, manifest, root=paths.managed_root, bin_dir=paths.managed_bin,
        python=sys.executable, production=True,
    )
    tx, receipt = activation.new_transaction(
        paths, source={"commit": "a" * 40, "tree": "b" * 40,
                       "branch": "fixture", "origin_main": "a" * 40},
    )
    receipt.update({"status": "rolled-back", "mutation_started": True})
    activation._write(tx / "receipt.json", receipt)
    paths.database.parent.mkdir(exist_ok=True)
    paths.database.write_bytes(b"unrelated session database sentinel")
    paths.unit_dir.mkdir(exist_ok=True)
    unit = activation._watchdog_unit(paths, tx)
    (paths.unit_dir / activation.WATCHDOG_UNIT).write_text(unit)
    kwargs = activation._release_wrapper_kwargs(paths, str(release["release_id"]))
    command = activation.service_wrappers.build_start_command(activation.WATCHDOG_UNIT, **kwargs)
    assert str(paths.managed_root / "releases" / str(release["release_id"]) / "site") in command
    before = {p: p.read_bytes() for p in (paths.database, tx / "receipt.json", paths.evidence / "LATEST")}
    environment = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-termdeck-fixture",
                   "XDG_RUNTIME_DIR": str(tmp_path / "no-service-bus")}
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(2):
            completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15)
            assert completed.returncode == 0, completed.stderr
            assert json.loads(completed.stdout) == 0
            assert unrelated.poll() is None
            assert {p: p.read_bytes() for p in before} == before
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_managed_release_identity_rejects_a_dangling_state_symlink(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.managed_root.mkdir()
    state = paths.managed_root / "state.json"
    state.symlink_to(tmp_path / "missing-state.json")

    with pytest.raises(activation.WebActivationError):
        activation._managed_release_id(paths)


def test_stage_cargo_binary_accepts_only_its_private_build_hardlink(tmp_path: Path) -> None:
    cargo_target = tmp_path / "cargo-target"
    release = cargo_target / "release"
    deps = release / "deps"
    deps.mkdir(parents=True)
    binary = release / "termdeck-web-server"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    peer = deps / "termdeck_web_server-test"
    os.link(binary, peer)
    staged = tmp_path / "staged" / "termdeck-web-server"

    digest = activation._stage_cargo_binary(binary, cargo_target, staged)

    assert digest == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert staged.read_bytes() == binary.read_bytes()
    assert staged.stat().st_nlink == 1
    assert staged.stat().st_ino != binary.stat().st_ino
    assert stat.S_IMODE(staged.stat().st_mode) == 0o755

    peer.unlink()
    outsider = tmp_path / "foreign-hardlink"
    os.link(binary, outsider)
    with pytest.raises(activation.WebActivationError, match="not its build artifact"):
        activation._stage_cargo_binary(binary, cargo_target, tmp_path / "bad")

    binary.unlink()
    binary.symlink_to(outsider)
    with pytest.raises(activation.WebActivationError):
        activation._stage_cargo_binary(binary, cargo_target, tmp_path / "symlink-bad")


def _tls_preservation_admission_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_termdeck_web_prepare_runtime import LAN_IP, _make_layout, _run

    layout = _make_layout(tmp_path / "owned-real-TLS")
    result = _run(layout)
    assert result.returncode == 0, result.stderr
    paths = dataclasses.replace(_paths(tmp_path), runtime=layout.runtime)
    receipt = json.loads((layout.runtime / "build-receipt.json").read_bytes())
    current = receipt["tls_publication"]["current"]
    publication = {
        "generation": receipt["tls_publication"]["generation"],
        "tls": {
            "path": current["path"],
            "identity": current["identity"],
            "manifest": {
                name: {**claim, "path": name}
                for name, claim in current["manifest"].items()
            },
        },
    }
    # This fixture isolates admission from the already separately qualified
    # signed broker boundary. Its certificates and descriptor checks are real.
    monkeypatch.setattr(activation.service_wrappers, "CANONICAL_RUNTIME_ROOT", paths.runtime)
    monkeypatch.setattr(
        activation, "_installed_tls_reuse_publisher_sha256", lambda _paths: "a" * 64,
    )
    monkeypatch.setattr(
        activation, "_broker_runtime_receipt", lambda _paths: (receipt, None, publication),
    )
    monkeypatch.setattr(activation, "_observed_primary_lan_ipv4", lambda _paths: LAN_IP)
    return paths


def test_tls_preservation_admission_accepts_actual_owned_certificate_and_inodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tls_preservation_admission_fixture(tmp_path, monkeypatch)
    before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in (paths.runtime / "tls").iterdir()}

    result = activation._require_preserved_tls_preparation(paths)

    assert result is not None
    activation._runtime_tls_publication(paths, result["consumer_tls"])
    assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in (paths.runtime / "tls").iterdir()} == before


def test_tls_preservation_admission_refuses_route_rotation_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tls_preservation_admission_fixture(tmp_path, monkeypatch)
    before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in (paths.runtime / "tls").iterdir()}
    monkeypatch.setattr(activation, "_observed_primary_lan_ipv4", lambda _paths: "192.168.50.10")

    with pytest.raises(activation.WebActivationError, match="changed LAN address"):
        activation._require_preserved_tls_preparation(paths)

    assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in (paths.runtime / "tls").iterdir()} == before


def test_tls_preservation_admission_refuses_expiring_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tls_preservation_admission_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        activation.subprocess, "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, b"", b""),
    )

    with pytest.raises(activation.WebActivationError, match="expiring protected TLS"):
        activation._require_preserved_tls_preparation(paths)


def test_tls_preservation_admission_preserves_concurrent_certificate_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tls_preservation_admission_fixture(tmp_path, monkeypatch)
    original_run = activation.subprocess.run
    certificate = paths.runtime / "tls" / "server.crt"

    def replace_after_expiry_check(argv, **kwargs):
        result = original_run(argv, **kwargs)
        certificate.write_bytes(b"owned concurrent writer")
        return result

    monkeypatch.setattr(activation.subprocess, "run", replace_after_expiry_check)
    with pytest.raises(activation.WebActivationError):
        activation._require_preserved_tls_preparation(paths)
    assert certificate.read_bytes() == b"owned concurrent writer"


@pytest.mark.parametrize(
    ("route", "addresses", "expected"),
    (
        ("1.1.1.1 via 192.168.50.1 dev test0 src 192.168.50.9\n", None, "192.168.50.9"),
        ("", "2: test0 inet 192.168.50.10/24 scope global test0\n", "192.168.50.10"),
        ("1.1.1.1 dev test0 src 8.8.8.8\n", None, None),
        ("", "", None),
    ),
)
def test_tls_preservation_primary_ip_matches_fixed_preparer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route, addresses, expected,
) -> None:
    calls = []

    def observed(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 5
        output = route if argv[2:4] == ["route", "get"] else addresses
        return subprocess.CompletedProcess(argv, 0, output or "", "")

    monkeypatch.setattr(activation, "_run", observed)
    if expected is None:
        with pytest.raises(activation.WebActivationError):
            activation._observed_primary_lan_ipv4(_paths(tmp_path))
    else:
        assert activation._observed_primary_lan_ipv4(_paths(tmp_path)) == expected
    assert len(calls) == (1 if addresses is None else 2)


def test_tls_preservation_publisher_refuses_user_owned_image(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "owned-publisher-fixture"
    activation._atomic(image, b"owned test image\n", 0o755)
    _, capture = activation._read_regular(image, label="owned publisher fixture")
    monkeypatch.setattr(activation, "_read_regular", lambda *_args, **_kwargs: (image, capture))

    with pytest.raises(activation.WebActivationError, match="ownership or identity"):
        activation._installed_tls_reuse_publisher_sha256(_paths(tmp_path))


def test_execute_tls_admission_precedes_recovery_package_and_service_mutations(
    tmp_path: Path, monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(
        activation, "load_transaction", lambda *_args: (tmp_path / "tx", {"status": "artifacts-staged"}),
    )

    def refuse(_paths):
        raise activation.WebActivationError("owned TLS admission refusal")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("TLS rejection must precede any recovery or package mutation")

    from termdeck import legacy_activation

    monkeypatch.setattr(activation, "_require_preserved_tls_preparation", refuse)
    monkeypatch.setattr(legacy_activation, "prepare", forbidden)
    monkeypatch.setattr(activation, "_install", forbidden)
    monkeypatch.setattr(activation, "_systemd", forbidden)

    with pytest.raises(activation.WebActivationError, match="owned TLS admission refusal"):
        activation.execute(paths, tmp_path / "tx")


def test_tls_preservation_crypto_uses_fixed_environment(tmp_path: Path, monkeypatch) -> None:
    paths = _tls_preservation_admission_fixture(tmp_path, monkeypatch)
    original_run = activation.subprocess.run
    monkeypatch.setenv("OPENSSL_CONF", str(tmp_path / "untrusted-caller-configuration"))
    calls = []

    def check_environment(argv, **kwargs):
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        calls.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(activation.subprocess, "run", check_environment)

    assert activation._require_preserved_tls_preparation(paths) is not None
    assert calls == [["/usr/bin/openssl", "x509", "-noout", "-checkend", "2592000"]]
