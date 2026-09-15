from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

import pytest

from termdeck import runtime_attestation


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "termdeck-preparer-state.py"
TRANSACTION_ID = "a" * 32
SHA256 = "b" * 64


def _load_helper() -> ModuleType:
    name = "termdeck_preparer_state_test_subject"
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _core() -> dict[str, object]:
    files = {
        name: SHA256
        for name in (
            "bin/termdeck-web-server",
            "nginx/nginx.conf",
            "tls/ca.crt",
            "tls/server-chain.crt",
            "tls/server.crt",
        )
    }
    outputs: dict[str, object] = {}
    for inode, name in enumerate(
        ("current-lan-ip", "nginx/allowed-hosts.map"), start=100
    ):
        outputs[name] = {
            "path": name,
            "sha256": SHA256,
            "mode": 0o600,
            "identity": [1, inode, 0o600, 1, 12, 20, 21],
        }
    manifest: dict[str, object] = {}
    modes = {
        "ca.crt": 0o600,
        "ca.key": 0o600,
        "server-chain.crt": 0o644,
        "server.crt": 0o644,
        "server.ext": 0o600,
        "server.key": 0o600,
    }
    for inode, name in enumerate(modes, start=200):
        mode = modes[name]
        manifest[name] = {
            "identity": [1, inode, mode, 1, 12, 20, 21],
            "mode": mode,
            "sha256": SHA256,
        }
    return {
        "files": files,
        "https_port": 7443,
        "lan_ip": "192.168.50.9",
        "ok": True,
        "runtime_publication": {
            "generation": TRANSACTION_ID,
            "outputs": outputs,
        },
        "schema": runtime_attestation.REQUEST_SCHEMA,
        "tls_publication": {
            "generation": TRANSACTION_ID,
            "current": {
                "path": "tls",
                "identity": [1, 300, 0o700, os.geteuid()],
                "manifest": manifest,
            },
            "prior": None,
        },
    }


def _write_core(path: Path, core: Mapping[str, object] | None = None) -> bytes:
    payload = helper._canonical_json(_core() if core is None else core)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        os.fchmod(descriptor, 0o644)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            assert written > 0
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return payload


def _identity(path: Path) -> tuple[int, int, int, int, int, int, int]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _config(tmp_path: Path) -> runtime_attestation.DaemonConfig:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.mkdir(mode=runtime_attestation.CONTROL_DIRECTORY_MODE)
    control.chmod(runtime_attestation.CONTROL_DIRECTORY_MODE)
    records = control / "records"
    records.mkdir(mode=runtime_attestation.RECORDS_DIRECTORY_MODE)
    records.chmod(runtime_attestation.RECORDS_DIRECTORY_MODE)
    config = runtime_attestation.DaemonConfig(
        control_root=control,
        socket_path=control / "runtime-attestd.sock",
        records_path=records,
        private_key_path=control / "runtime-attestd.key",
        public_key_path=tmp_path / "runtime-attestd.pub",
        runtime_receipt_path=runtime / "build-receipt.json",
        preparer=runtime_attestation.Principal(
            "test-preparer", os.geteuid(), os.getegid()
        ),
        root_uid=os.geteuid(),
        root_gid=os.getegid(),
    )
    runtime_attestation.generate_keypair(config)
    return config


def _daemon_signer(
    config: runtime_attestation.DaemonConfig,
    *,
    after_sign: Callable[[int], None] | None = None,
    seen: dict[str, object] | None = None,
):
    daemon = runtime_attestation.RuntimeAttestationDaemon(config)

    def sign(
        request: Mapping[str, object],
        descriptor: int,
        supplied_config: runtime_attestation.DaemonConfig,
    ) -> Mapping[str, object]:
        assert supplied_config is config
        before = os.fstat(descriptor)
        if seen is not None:
            seen["descriptor"] = descriptor
            seen["identity"] = (before.st_dev, before.st_ino)
            offset = os.lseek(descriptor, 0, os.SEEK_CUR)
            os.lseek(descriptor, 0, os.SEEK_SET)
            seen["content"] = os.read(descriptor, config.max_receipt_bytes)
            os.lseek(descriptor, offset, os.SEEK_SET)
        response = daemon.attest(
            request,
            receipt_descriptor=descriptor,
            peer=runtime_attestation.PeerCredentials(
                os.getpid(), os.geteuid(), os.getegid()
            ),
        )
        if after_sign is not None:
            after_sign(descriptor)
        return response

    return sign


def test_finalizes_with_ed25519_attestation_on_the_published_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    before = _identity(config.runtime_receipt_path)
    seen: dict[str, object] = {}

    receipt = helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config, seen=seen),
    )

    after = _identity(config.runtime_receipt_path)
    assert after[:4] == before[:4]
    assert seen["identity"] == before[:2]
    assert seen["content"] == core_bytes
    assert "producer_commitment" not in receipt
    response = receipt["producer_attestation"]
    assert isinstance(response, dict)
    assert response["receipt_core_identity"] == list(before)
    assert response["attestation"]["algorithm"] == runtime_attestation.SIGNATURE_ALGORITHM
    runtime_attestation.verify_runtime_build_attestation(receipt, config=config)
    assert json.loads(config.runtime_receipt_path.read_bytes()) == receipt


def test_verify_existing_authenticates_receipt_after_current_sources_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    server = config.runtime_receipt_path.parent / "bin" / "termdeck-web-server"
    nginx = config.runtime_receipt_path.parent / "nginx" / "nginx.conf"
    server.parent.mkdir()
    nginx.parent.mkdir()
    server.write_bytes(b"signed server source\n")
    nginx.write_bytes(b"signed nginx source\n")
    core = _core()
    files = core["files"]
    assert isinstance(files, dict)
    files["bin/termdeck-web-server"] = hashlib.sha256(server.read_bytes()).hexdigest()
    files["nginx/nginx.conf"] = hashlib.sha256(nginx.read_bytes()).hexdigest()
    _write_core(config.runtime_receipt_path, core)
    final = helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    server.write_bytes(b"new unsigned server source\n")
    nginx.write_bytes(b"new unsigned nginx source\n")

    assert helper.verify_runtime_receipt(
        config.runtime_receipt_path,
        config=config,
    ) == final

    monkeypatch.setattr(helper.runtime_attestation, "production_config", lambda: config)
    assert helper.main(
        ["--verify-existing", "--receipt", str(config.runtime_receipt_path)]
    ) == 0
    assert json.loads(capsys.readouterr().out) == final


def test_verify_existing_accepts_renamed_same_inode_in_exact_tls_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    _write_core(config.runtime_receipt_path)
    final = helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    canonical_identity = _identity(config.runtime_receipt_path)
    journal = config.runtime_receipt_path.parent / (
        ".termdeck-tls-transaction-" + "c" * 32
    )
    journal.mkdir(mode=0o700)
    prior = journal / "prior-build-receipt.json"
    config.runtime_receipt_path.rename(prior)

    assert _identity(prior)[:4] == canonical_identity[:4]
    verified = helper.verify_runtime_receipt(prior, config=config)
    assert verified == final
    response = verified["producer_attestation"]
    assert isinstance(response, dict)
    assert response["receipt_path"] == str(config.runtime_receipt_path)

    monkeypatch.setattr(helper.runtime_attestation, "production_config", lambda: config)
    assert helper.main(["--verify-existing", "--receipt", str(prior)]) == 0
    assert json.loads(capsys.readouterr().out) == final


def test_verify_existing_accepts_inverse_rollback_prior_receipt(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_core(config.runtime_receipt_path)
    final = helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    canonical_identity = _identity(config.runtime_receipt_path)
    journal = config.runtime_receipt_path.parent / (
        ".termdeck-tls-transaction-" + "e" * 32
    )
    journal.mkdir(mode=0o700)
    rollback_prior = journal / "rollback-prior-build-receipt.json"
    config.runtime_receipt_path.rename(rollback_prior)

    assert _identity(rollback_prior)[:4] == canonical_identity[:4]
    verified = helper.verify_runtime_receipt(rollback_prior, config=config)
    assert verified == final
    response = verified["producer_attestation"]
    assert isinstance(response, dict)
    assert response["receipt_path"] == str(config.runtime_receipt_path)


@pytest.mark.parametrize("location", ("sibling", "leaf", "journal"))
def test_verify_existing_rejects_arbitrary_sibling_leaf_or_journal(
    tmp_path: Path,
    location: str,
) -> None:
    config = _config(tmp_path)
    _write_core(config.runtime_receipt_path)
    helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    runtime = config.runtime_receipt_path.parent
    if location == "sibling":
        candidate = runtime / "prior-build-receipt.json"
    elif location == "leaf":
        journal = runtime / (".termdeck-tls-transaction-" + "d" * 32)
        journal.mkdir(mode=0o700)
        candidate = journal / "build-receipt.json"
    else:
        journal = runtime / (".termdeck-tls-transaction-" + "E" * 32)
        journal.mkdir(mode=0o700)
        candidate = journal / "prior-build-receipt.json"
    config.runtime_receipt_path.rename(candidate)

    with pytest.raises(helper.ReceiptStateError, match="verification path"):
        helper.verify_runtime_receipt(candidate, config=config)


@pytest.mark.parametrize("unsafe", ("mode", "link"))
def test_verify_existing_rejects_unsafe_mode_or_link_count(
    tmp_path: Path,
    unsafe: str,
) -> None:
    config = _config(tmp_path)
    _write_core(config.runtime_receipt_path)
    helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    if unsafe == "mode":
        config.runtime_receipt_path.chmod(0o600)
    else:
        os.link(config.runtime_receipt_path, tmp_path / "second-final-link")

    with pytest.raises(helper.ReceiptStateError, match="mode 0644 and one link"):
        helper.verify_runtime_receipt(config.runtime_receipt_path, config=config)


def test_verify_existing_rejects_hmac_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    legacy = {
        **_core(),
        "producer_commitment": {
            "algorithm": "hmac-sha256-v1",
            "receipt_identity": [1, 2, 0o644, 1],
            "source_inputs": {},
            "mac": "0" * 64,
        },
    }
    _write_core(config.runtime_receipt_path, legacy)

    with pytest.raises(helper.ReceiptStateError, match="HMAC producer_commitment"):
        helper.verify_runtime_receipt(config.runtime_receipt_path, config=config)


def test_verify_existing_rejects_path_replacement_during_signature_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    helper.finalize_runtime_receipt(
        config.runtime_receipt_path,
        TRANSACTION_ID,
        config=config,
        signer=_daemon_signer(config),
    )
    final_bytes = config.runtime_receipt_path.read_bytes()
    final_identity = _identity(config.runtime_receipt_path)
    detached = tmp_path / "detached-final-receipt"
    verify = runtime_attestation.verify_runtime_build_attestation

    def verify_then_replace(
        receipt: object,
        *,
        config: runtime_attestation.DaemonConfig,
    ) -> dict[str, object]:
        result = verify(receipt, config=config)
        config.runtime_receipt_path.rename(detached)
        _write_core(config.runtime_receipt_path)
        return result

    monkeypatch.setattr(
        helper.runtime_attestation,
        "verify_runtime_build_attestation",
        verify_then_replace,
    )
    with pytest.raises(helper.ReceiptStateError, match="path was replaced"):
        helper.verify_runtime_receipt(config.runtime_receipt_path, config=config)

    assert _identity(detached)[:2] == final_identity[:2]
    assert detached.read_bytes() == final_bytes
    assert config.runtime_receipt_path.read_bytes() == core_bytes


@pytest.mark.parametrize(
    "arguments",
    (
        ["--receipt", "/tmp/build-receipt.json"],
        [
            "--receipt",
            "/tmp/build-receipt.json",
            "--verify-existing",
            "--transaction-id",
            TRANSACTION_ID,
        ],
    ),
)
def test_cli_requires_exactly_one_finalize_or_verify_mode(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as failure:
        helper.main(arguments)
    assert failure.value.code == 2


def test_signing_failure_leaves_exact_unsigned_core_on_same_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    before = _identity(config.runtime_receipt_path)
    seen: dict[str, object] = {}

    def refuse(
        _request: Mapping[str, object],
        descriptor: int,
        _config: runtime_attestation.DaemonConfig,
    ) -> Mapping[str, object]:
        info = os.fstat(descriptor)
        seen["identity"] = (info.st_dev, info.st_ino)
        raise runtime_attestation.AttestationError("test signer refusal")

    with pytest.raises(helper.ReceiptStateError, match="Ed25519 attestation failed"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=refuse,
        )

    assert seen["identity"] == before[:2]
    assert _identity(config.runtime_receipt_path) == before
    assert config.runtime_receipt_path.read_bytes() == core_bytes
    assert "producer_attestation" not in json.loads(core_bytes)


def test_invalid_ed25519_response_leaves_exact_unsigned_core_on_same_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    before = _identity(config.runtime_receipt_path)
    real_signer = _daemon_signer(config)

    def forge(
        request: Mapping[str, object],
        descriptor: int,
        supplied_config: runtime_attestation.DaemonConfig,
    ) -> Mapping[str, object]:
        response = json.loads(
            json.dumps(real_signer(request, descriptor, supplied_config))
        )
        signature = response["attestation"]["signature"]
        response["attestation"]["signature"] = (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        return response

    with pytest.raises(helper.ReceiptStateError, match="Ed25519 attestation failed"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=forge,
        )

    assert _identity(config.runtime_receipt_path) == before
    assert config.runtime_receipt_path.read_bytes() == core_bytes


def test_rejects_hmac_producer_commitment_without_calling_signer(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    legacy = {
        **_core(),
        "producer_commitment": {
            "algorithm": "hmac-sha256-v1",
            "receipt_identity": [1, 2, 0o644, 1],
            "source_inputs": {},
            "mac": "0" * 64,
        },
    }
    core_bytes = _write_core(config.runtime_receipt_path, legacy)
    before = _identity(config.runtime_receipt_path)

    def unexpected(*_arguments: object) -> Mapping[str, object]:
        pytest.fail("HMAC receipt reached the signer")

    with pytest.raises(helper.ReceiptStateError, match="HMAC producer_commitment"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=unexpected,
        )

    assert _identity(config.runtime_receipt_path) == before
    assert config.runtime_receipt_path.read_bytes() == core_bytes


def test_rejects_path_replacement_after_signing_without_finalizing_either_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    original = _identity(config.runtime_receipt_path)
    detached = tmp_path / "detached-original-receipt"

    def replace(_descriptor: int) -> None:
        config.runtime_receipt_path.rename(detached)
        _write_core(config.runtime_receipt_path)

    with pytest.raises(helper.ReceiptStateError, match="path was replaced"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=_daemon_signer(config, after_sign=replace),
        )

    assert _identity(detached)[:2] == original[:2]
    assert _identity(config.runtime_receipt_path)[:2] != original[:2]
    assert detached.read_bytes() == core_bytes
    assert config.runtime_receipt_path.read_bytes() == core_bytes
    assert "producer_attestation" not in json.loads(detached.read_bytes())
    assert "producer_attestation" not in json.loads(config.runtime_receipt_path.read_bytes())


def test_rejects_parent_replacement_after_signing_without_finalizing_old_tree(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    original = _identity(config.runtime_receipt_path)
    detached_runtime = tmp_path / "detached-runtime"

    def replace_parent(_descriptor: int) -> None:
        config.runtime_receipt_path.parent.rename(detached_runtime)
        config.runtime_receipt_path.parent.mkdir(mode=0o700)
        _write_core(config.runtime_receipt_path)

    with pytest.raises(helper.ReceiptStateError, match="parent path was replaced"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=_daemon_signer(config, after_sign=replace_parent),
        )

    detached_receipt = detached_runtime / "build-receipt.json"
    assert _identity(detached_receipt)[:2] == original[:2]
    assert _identity(config.runtime_receipt_path)[:2] != original[:2]
    assert detached_receipt.read_bytes() == core_bytes
    assert config.runtime_receipt_path.read_bytes() == core_bytes
    assert "producer_attestation" not in json.loads(detached_receipt.read_bytes())


@pytest.mark.parametrize("unsafe", ("mode", "link"))
def test_rejects_unsafe_mode_or_link_count_before_signing(
    tmp_path: Path,
    unsafe: str,
) -> None:
    config = _config(tmp_path)
    _write_core(config.runtime_receipt_path)
    if unsafe == "mode":
        config.runtime_receipt_path.chmod(0o600)
    else:
        os.link(config.runtime_receipt_path, tmp_path / "second-link")

    def unexpected(*_arguments: object) -> Mapping[str, object]:
        pytest.fail("unsafe receipt reached the signer")

    with pytest.raises(helper.ReceiptStateError, match="mode 0644 and one link"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=unexpected,
        )


def test_rejects_same_inode_mutation_after_signing_before_finalization(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    core_bytes = _write_core(config.runtime_receipt_path)
    before = _identity(config.runtime_receipt_path)

    def mutate(descriptor: int) -> None:
        os.lseek(descriptor, 2, os.SEEK_SET)
        os.write(descriptor, b"g")
        os.fsync(descriptor)

    with pytest.raises(helper.ReceiptStateError, match="core changed"):
        helper.finalize_runtime_receipt(
            config.runtime_receipt_path,
            TRANSACTION_ID,
            config=config,
            signer=_daemon_signer(config, after_sign=mutate),
        )

    after = _identity(config.runtime_receipt_path)
    assert after[:4] == before[:4]
    assert after[4] == before[4]
    assert config.runtime_receipt_path.read_bytes() != core_bytes
    assert "producer_attestation" not in json.loads(config.runtime_receipt_path.read_bytes())
