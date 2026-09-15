from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from termdeck import runtime_attestation
from test_termdeck_web_prepare_runtime import (
    LAN_IP,
    RuntimeLayout,
    _assert_tls_contract,
    _make_layout,
    _run,
    _set_fake_ip,
)


B_LAN_IP = "192.168.50.10"
PRODUCTION_PUBLIC_KEY = "/etc/termdeck/runtime-attestd.pub"


@dataclass(frozen=True)
class AttestationFixture:
    config: runtime_attestation.DaemonConfig
    observations: Path
    consumed: Path
    mode: Path


@dataclass(frozen=True)
class ActiveFile:
    identity: tuple[int, int, int, int, int, int]
    content: bytes


@dataclass(frozen=True)
class ActiveRuntime:
    files: dict[str, ActiveFile]
    tls_identity: tuple[int, int, int, int]
    receipt_bytes: bytes
    receipt: dict[str, object]


def _attestor_config(
    layout: RuntimeLayout, fixture_root: Path
) -> runtime_attestation.DaemonConfig:
    control = fixture_root / "control"
    records = control / "records"
    return runtime_attestation.DaemonConfig(
        control_root=control,
        socket_path=control / "runtime-attestd.sock",
        records_path=records,
        private_key_path=control / "runtime-attestd.key",
        public_key_path=fixture_root / "runtime-attestd.pub",
        runtime_receipt_path=layout.runtime / "build-receipt.json",
        preparer=runtime_attestation.Principal(
            "fixture-preparer", os.geteuid(), os.getegid()
        ),
        root_uid=os.geteuid(),
        root_gid=os.getegid(),
    )


def _install_attestation_fixture(
    layout: RuntimeLayout, fixture_root: Path
) -> AttestationFixture:
    """Replace only the copied script's fixed helper with a disposable attestor."""

    config = _attestor_config(layout, fixture_root)
    config.control_root.mkdir(
        mode=runtime_attestation.CONTROL_DIRECTORY_MODE, parents=True
    )
    config.control_root.chmod(runtime_attestation.CONTROL_DIRECTORY_MODE)
    config.records_path.mkdir(mode=runtime_attestation.RECORDS_DIRECTORY_MODE)
    config.records_path.chmod(runtime_attestation.RECORDS_DIRECTORY_MODE)
    runtime_attestation.generate_keypair(config)
    observations = fixture_root / "attestor-observations.jsonl"
    consumed = fixture_root / "attestor-fault-consumed"
    mode_path = fixture_root / "attestor-mode"
    mode_path.write_text("success\n", encoding="ascii")
    helper = fixture_root / "termdeck-preparer-state-fixture"
    helper.write_text(
        f"""#!/usr/bin/python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path({str(Path(__file__).resolve().parents[1])!r})
STATE_HELPER = PROJECT_ROOT / "scripts" / "termdeck-preparer-state.py"
sys.path.insert(0, str(PROJECT_ROOT))

from termdeck import runtime_attestation


def load_state_helper():
    spec = importlib.util.spec_from_file_location(
        "termdeck_preparer_state_attestation_rollback_fixture", STATE_HELPER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load receipt finalizer fixture subject")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\\n"
    ).encode("utf-8")


def write_regular(path, content):
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        os.fchmod(descriptor, 0o644)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("fixture replacement write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


parser = argparse.ArgumentParser()
parser.add_argument("--receipt", required=True)
operation = parser.add_mutually_exclusive_group(required=True)
operation.add_argument("--transaction-id")
operation.add_argument("--verify-existing", action="store_true")
arguments = parser.parse_args()
receipt_path = Path(arguments.receipt)
fixture_root = Path({str(fixture_root)!r})
observations = Path({str(observations)!r})
consumed = Path({str(consumed)!r})
mode = Path({str(mode_path)!r}).read_text(encoding="ascii").strip()
control = fixture_root / "control"
config = runtime_attestation.DaemonConfig(
    control_root=control,
    socket_path=control / "runtime-attestd.sock",
    records_path=control / "records",
    private_key_path=control / "runtime-attestd.key",
    public_key_path=fixture_root / "runtime-attestd.pub",
    runtime_receipt_path=Path({str(config.runtime_receipt_path)!r}),
    preparer=runtime_attestation.Principal(
        "fixture-preparer", os.geteuid(), os.getegid()
    ),
    root_uid=os.geteuid(),
    root_gid=os.getegid(),
)
unsigned = receipt_path.read_bytes()
core = json.loads(unsigned)
before = receipt_path.stat()
fault = (
    not arguments.verify_existing
    and mode != "success"
    and core.get("lan_ip") == {B_LAN_IP!r}
    and not consumed.exists()
)
with observations.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{
        "fault": fault,
        "mode": mode,
        "operation": "verify" if arguments.verify_existing else "finalize",
        "transaction_id": arguments.transaction_id,
        "lan_ip": core.get("lan_ip"),
        "receipt_identity": [before.st_dev, before.st_ino],
        "unsigned_sha256": hashlib.sha256(unsigned).hexdigest(),
    }}, sort_keys=True) + "\\n")
    stream.flush()
    os.fsync(stream.fileno())
if fault:
    consumed.write_text(mode + "\\n", encoding="ascii")

if fault and mode == "broker-refusal":
    sys.stderr.write("injected root attestation broker refusal\\n")
    raise SystemExit(73)

state_helper = load_state_helper()
if arguments.verify_existing:
    verified = state_helper.verify_runtime_receipt(receipt_path, config=config)
    sys.stdout.buffer.write(canonical(verified))
    raise SystemExit(0)

daemon = runtime_attestation.RuntimeAttestationDaemon(config)


def signer(request, descriptor, supplied_config):
    if supplied_config is not config:
        raise RuntimeError("fixture received another attestor configuration")
    if fault and mode == "signer-failure":
        raise runtime_attestation.AttestationError("injected signer failure")
    response = daemon.attest(
        request,
        receipt_descriptor=descriptor,
        peer=runtime_attestation.PeerCredentials(
            os.getpid(), os.geteuid(), os.getegid()
        ),
    )
    if fault and mode == "path-inode-swap":
        detached = receipt_path.with_name(
            ".attestation-path-swap-original-" + arguments.transaction_id
        )
        os.rename(receipt_path, detached)
        write_regular(receipt_path, unsigned)
        parent = os.open(
            receipt_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    elif fault and mode == "fd-inode-swap":
        replacement = receipt_path.with_name(
            ".attestation-fd-swap-" + arguments.transaction_id
        )
        write_regular(replacement, unsigned)
        replacement_descriptor = os.open(
            replacement, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.dup2(replacement_descriptor, descriptor)
        finally:
            os.close(replacement_descriptor)
    return response


if fault and mode == "interrupted-finalization":
    def interrupted_write(descriptor, content):
        prefix = content[: max(1, len(content) // 3)]
        written = os.write(descriptor, prefix)
        if written != len(prefix):
            raise RuntimeError("fixture partial write was incomplete")
        os.fsync(descriptor)
        raise KeyboardInterrupt("injected receipt finalization interruption")

    state_helper._write_all = interrupted_write

try:
    finalized = state_helper.finalize_runtime_receipt(
        receipt_path,
        arguments.transaction_id,
        config=config,
        signer=signer,
    )
except BaseException as exc:
    sys.stderr.write(f"fixture attestation failed: {{type(exc).__name__}}: {{exc}}\\n")
    raise SystemExit(74)
sys.stdout.buffer.write(canonical(finalized))
""",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    script_text = layout.script.read_text(encoding="utf-8")
    # The shared layout has already replaced the production literal with its
    # generic helper.  Replace that copied path again for this fault fixture.
    assert str(layout.attest_helper) in script_text
    script_text = script_text.replace(str(layout.attest_helper), str(helper))
    # The copied producer verifies retained and newly finalized receipts with
    # the public half of the same disposable key.  Production keeps its fixed
    # /etc path; only this copied test executable is rewritten.
    script_text = script_text.replace(PRODUCTION_PUBLIC_KEY, str(config.public_key_path))
    layout.script.write_text(script_text, encoding="utf-8")
    layout.script.chmod(0o755)
    return AttestationFixture(config, observations, consumed, mode_path)


def _file(path: Path) -> ActiveFile:
    observed = path.stat()
    return ActiveFile(
        (
            observed.st_dev,
            observed.st_ino,
            stat.S_IMODE(observed.st_mode),
            observed.st_nlink,
            observed.st_size,
            observed.st_mtime_ns,
        ),
        path.read_bytes(),
    )


def _active_runtime(layout: RuntimeLayout) -> ActiveRuntime:
    relatives = (
        "build-receipt.json",
        "current-lan-ip",
        "nginx/allowed-hosts.map",
        "bin/termdeck-web-server",
        "nginx/nginx.conf",
        "tls/ca.crt",
        "tls/ca.key",
        "tls/ca.srl",
        "tls/server.key",
        "tls/server.crt",
        "tls/server-chain.crt",
        "tls/server.ext",
    )
    receipt_bytes = (layout.runtime / "build-receipt.json").read_bytes()
    tls = (layout.runtime / "tls").stat()
    return ActiveRuntime(
        files={relative: _file(layout.runtime / relative) for relative in relatives},
        tls_identity=(
            tls.st_dev,
            tls.st_ino,
            stat.S_IMODE(tls.st_mode),
            tls.st_uid,
        ),
        receipt_bytes=receipt_bytes,
        receipt=json.loads(receipt_bytes),
    )


def _observations(fixture: AttestationFixture) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in fixture.observations.read_text(encoding="utf-8").splitlines()
    ]


def _paths_with_inode(root: Path, inode: int) -> list[Path]:
    result: list[Path] = []
    for candidate in root.rglob("*"):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_ino == inode:
            result.append(candidate)
    return result


def _assert_receipt_describes_active_runtime(
    layout: RuntimeLayout,
    fixture: AttestationFixture,
    *,
    lan_ip: str,
) -> dict[str, object]:
    receipt = json.loads(
        (layout.runtime / "build-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["lan_ip"] == lan_ip
    assert "producer_attestation" in receipt
    assert "producer_commitment" not in receipt
    runtime_attestation.verify_runtime_build_attestation(
        receipt, config=fixture.config
    )
    for relative, entry in receipt["runtime_publication"]["outputs"].items():
        path = layout.runtime / relative
        observed = path.stat()
        assert entry["identity"] == [
            observed.st_dev,
            observed.st_ino,
            stat.S_IMODE(observed.st_mode),
            observed.st_nlink,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    return receipt


def _assert_failed_publication_evidence(
    layout: RuntimeLayout,
    fixture: AttestationFixture,
    mode: str,
) -> dict[str, object]:
    assert fixture.consumed.read_text(encoding="ascii") == mode + "\n"
    observed = _observations(fixture)
    failures = [entry for entry in observed if entry["fault"] is True]
    assert len(failures) == 1
    failed_inode = failures[0]["receipt_identity"][1]

    # Before asking the producer to perform its explicit inverse, prove that
    # the failed B receipt was removed from the canonical public name and
    # durably retained.  A path-swap fault may leave an untrusted replacement
    # at the public name, but the helper-observed B inode remains private.
    public = layout.runtime / "build-receipt.json"
    if public.exists():
        assert public.stat().st_ino != failed_inode
    retained = _paths_with_inode(layout.runtime, failed_inode)
    assert retained
    assert public not in retained
    return failures[0]


def _assert_rolled_back(
    layout: RuntimeLayout,
    fixture: AttestationFixture,
    active_a: ActiveRuntime,
    failure: dict[str, object],
) -> None:
    failed_inode = failure["receipt_identity"][1]

    # A's active leaves return on their original inodes.  Renaming an inode
    # during inverse publication can change ctime, which is why rollback may
    # reissue the public A receipt; all other stable identity fields and bytes
    # must survive exactly.
    for relative, expected in active_a.files.items():
        if relative == "build-receipt.json":
            continue
        assert _file(layout.runtime / relative) == expected
    tls = (layout.runtime / "tls").stat()
    assert (
        tls.st_dev,
        tls.st_ino,
        stat.S_IMODE(tls.st_mode),
        tls.st_uid,
    ) == active_a.tls_identity
    _assert_tls_contract(layout, LAN_IP)
    receipt = _assert_receipt_describes_active_runtime(
        layout, fixture, lan_ip=LAN_IP
    )
    assert receipt["files"] == active_a.receipt["files"]
    assert receipt["tls_publication"]["current"] == (
        active_a.receipt["tls_publication"]["current"]
    )

    # Preserve the exact formerly-active signed receipt as rollback evidence,
    # even when current output ctimes require a freshly attested A receipt.
    assert any(
        candidate.read_bytes() == active_a.receipt_bytes
        for candidate in layout.runtime.rglob("*")
        if candidate.is_file() and not candidate.is_symlink()
    )

    # The helper-observed B inode must no longer occupy the public receipt
    # name, but it remains private evidence even when finalization was cut off
    # after truncation and a partial write.
    public = layout.runtime / "build-receipt.json"
    assert public.stat().st_ino != failed_inode
    retained = _paths_with_inode(layout.runtime, failed_inode)
    assert retained
    assert public not in retained


def _recover_unreceipted_generation(layout: RuntimeLayout) -> None:
    recovered = _run(layout, arguments=["--restore-unreceipted-tls-generation"])
    assert recovered.returncode == 0, recovered.stderr


@pytest.fixture
def attested_a(tmp_path: Path) -> tuple[RuntimeLayout, AttestationFixture, ActiveRuntime]:
    layout = _make_layout(tmp_path)
    fixture = _install_attestation_fixture(layout, tmp_path / "attestor")
    published = _run(layout)
    assert published.returncode == 0, published.stderr
    active = _active_runtime(layout)
    _assert_receipt_describes_active_runtime(layout, fixture, lan_ip=LAN_IP)
    return layout, fixture, active


@pytest.mark.parametrize("mode", ("signer-failure", "broker-refusal"))
def test_attestation_refusal_rolls_back_to_the_previous_active_runtime_and_receipts(
    attested_a: tuple[RuntimeLayout, AttestationFixture, ActiveRuntime],
    mode: str,
) -> None:
    layout, fixture, active_a = attested_a
    _set_fake_ip(layout, B_LAN_IP)
    fixture.mode.write_text(mode + "\n", encoding="ascii")

    failed = _run(layout)

    assert failed.returncode != 0
    failure = _assert_failed_publication_evidence(layout, fixture, mode)
    _recover_unreceipted_generation(layout)
    _assert_rolled_back(layout, fixture, active_a, failure)


@pytest.mark.parametrize("mode", ("fd-inode-swap", "path-inode-swap"))
def test_attestation_inode_swap_rolls_back_without_accepting_the_replacement(
    attested_a: tuple[RuntimeLayout, AttestationFixture, ActiveRuntime],
    mode: str,
) -> None:
    layout, fixture, active_a = attested_a
    _set_fake_ip(layout, B_LAN_IP)
    fixture.mode.write_text(mode + "\n", encoding="ascii")

    failed = _run(layout)

    assert failed.returncode != 0
    failure = _assert_failed_publication_evidence(layout, fixture, mode)
    _recover_unreceipted_generation(layout)
    _assert_rolled_back(layout, fixture, active_a, failure)


def test_interrupted_finalization_retains_partial_receipt_and_restores_active_a(
    attested_a: tuple[RuntimeLayout, AttestationFixture, ActiveRuntime],
) -> None:
    layout, fixture, active_a = attested_a
    _set_fake_ip(layout, B_LAN_IP)
    mode = "interrupted-finalization"
    fixture.mode.write_text(mode + "\n", encoding="ascii")

    failed = _run(layout)

    assert failed.returncode != 0
    failure = _assert_failed_publication_evidence(layout, fixture, mode)
    _recover_unreceipted_generation(layout)
    _assert_rolled_back(layout, fixture, active_a, failure)
    retained = _paths_with_inode(layout.runtime, failure["receipt_identity"][1])
    assert len(retained) == 1
    partial = retained[0].read_bytes()
    assert partial
    assert hashlib.sha256(partial).hexdigest() != failure["unsigned_sha256"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(partial)
