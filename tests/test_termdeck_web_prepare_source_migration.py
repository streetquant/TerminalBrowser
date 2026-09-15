from __future__ import annotations

import hashlib
import json
import os
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from termdeck import runtime_attestation

from test_termdeck_web_prepare_runtime import (
    LAN_IP,
    RuntimeLayout,
    _assert_published,
    _assert_tls_contract,
    _make_layout,
    _run,
    _set_fake_ip,
)


B_LAN_IP = "192.168.50.10"
B_SERVER = b"#!/usr/bin/bash\nprintf 'TermDeck B server\\n'\n"
B_CONFIG = b"# release B configuration\nserver {\n    listen 0.0.0.0:7443 ssl;\n}\n"
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ForwardSourceMigration:
    layout: RuntimeLayout
    attestation_config: runtime_attestation.DaemonConfig
    a_receipt_bytes: bytes
    a_receipt: dict[str, object]
    b_source_bytes: dict[str, bytes]


def _replace_regular(path: Path, content: bytes, mode: int) -> None:
    replacement = path.parent / ("." + path.name + ".release-b")
    replacement.write_bytes(content)
    replacement.chmod(mode)
    os.replace(replacement, path)


def _install_disposable_attest_helper(
    layout: RuntimeLayout,
) -> runtime_attestation.DaemonConfig:
    """Give the copied producer a real signer without starting a service."""

    control = layout.root / "attestation-control"
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
        public_key_path=layout.root / "runtime-attestd.pub",
        runtime_receipt_path=layout.runtime / "build-receipt.json",
        preparer=runtime_attestation.Principal(
            "test-preparer", os.geteuid(), os.getegid()
        ),
        root_uid=os.geteuid(),
        root_gid=os.getegid(),
    )
    runtime_attestation.generate_keypair(config)

    helper = layout.root / "termdeck-preparer-state-fixture"
    helper.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/python3
            import argparse
            import json
            import os
            import runpy
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(ROOT)!r})
            from termdeck import runtime_attestation

            arguments = argparse.ArgumentParser()
            arguments.add_argument("--receipt", required=True)
            mode = arguments.add_mutually_exclusive_group(required=True)
            mode.add_argument("--transaction-id")
            mode.add_argument("--verify-existing", action="store_true")
            parsed = arguments.parse_args()
            config = runtime_attestation.DaemonConfig(
                control_root=Path({str(config.control_root)!r}),
                socket_path=Path({str(config.socket_path)!r}),
                records_path=Path({str(config.records_path)!r}),
                private_key_path=Path({str(config.private_key_path)!r}),
                public_key_path=Path({str(config.public_key_path)!r}),
                runtime_receipt_path=Path({str(config.runtime_receipt_path)!r}),
                preparer=runtime_attestation.Principal(
                    "test-preparer", os.geteuid(), os.getegid()
                ),
                root_uid=os.geteuid(),
                root_gid=os.getegid(),
            )
            daemon = runtime_attestation.RuntimeAttestationDaemon(config)

            def signer(request, descriptor, supplied_config):
                assert supplied_config == config
                return daemon.attest(
                    request,
                    receipt_descriptor=descriptor,
                    peer=runtime_attestation.PeerCredentials(
                        os.getpid(), os.geteuid(), os.getegid()
                    ),
                )

            state = runpy.run_path({str(ROOT / 'scripts' / 'termdeck-preparer-state.py')!r})
            if parsed.verify_existing:
                receipt = state["verify_runtime_receipt"](
                    parsed.receipt,
                    config=config,
                )
            else:
                receipt = state["finalize_runtime_receipt"](
                    parsed.receipt,
                    parsed.transaction_id,
                    config=config,
                    signer=signer,
                )
            sys.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    helper.chmod(0o755)

    copied = layout.script.read_text(encoding="utf-8")
    fixture_helper = str(layout.attest_helper)
    assert fixture_helper in copied
    copied = copied.replace(fixture_helper, str(helper))
    public_key = str(runtime_attestation.DEFAULT_PUBLIC_KEY_PATH)
    if public_key in copied:
        copied = copied.replace(public_key, str(config.public_key_path))
    layout.script.write_text(copied, encoding="utf-8")
    layout.script.chmod(0o755)
    return config


@pytest.fixture
def forward_source_migration(tmp_path: Path) -> ForwardSourceMigration:
    """Publish A, then install distinct release-B preparer source leaves."""

    layout = _make_layout(tmp_path)
    attestation_config = _install_disposable_attest_helper(layout)
    published_a = _run(layout)
    assert published_a.returncode == 0, published_a.stderr
    _assert_published(layout, LAN_IP)
    _assert_tls_contract(layout, LAN_IP)

    receipt_path = layout.runtime / "build-receipt.json"
    a_receipt_bytes = receipt_path.read_bytes()
    a_receipt = json.loads(a_receipt_bytes)
    assert "producer_attestation" in a_receipt
    assert "producer_commitment" not in a_receipt

    sources = {
        "bin/termdeck-web-server": B_SERVER,
        "nginx/nginx.conf": B_CONFIG,
    }
    original_inodes = {
        relative: (layout.runtime / relative).stat().st_ino
        for relative in sources
    }
    _replace_regular(layout.runtime / "bin/termdeck-web-server", B_SERVER, 0o755)
    _replace_regular(layout.runtime / "nginx/nginx.conf", B_CONFIG, 0o644)
    for relative, content in sources.items():
        source = layout.runtime / relative
        assert source.stat().st_ino != original_inodes[relative]
        assert source.read_bytes() == content

    _set_fake_ip(layout, B_LAN_IP)
    return ForwardSourceMigration(
        layout, attestation_config, a_receipt_bytes, a_receipt, sources
    )


def test_attested_a_receipt_authorizes_forward_source_migration_to_b(
    forward_source_migration: ForwardSourceMigration,
) -> None:
    """A's signed provenance must survive a legitimate A-to-B preparation."""

    migration = forward_source_migration
    layout = migration.layout
    a_receipt = migration.a_receipt

    published_b = _run(layout)

    assert published_b.returncode == 0, published_b.stderr
    _assert_published(layout, B_LAN_IP)
    _assert_tls_contract(layout, B_LAN_IP)
    receipt_path = layout.runtime / "build-receipt.json"
    b_receipt = json.loads(receipt_path.read_bytes())
    assert "producer_attestation" in b_receipt
    assert "producer_commitment" not in b_receipt
    runtime_attestation.verify_runtime_build_attestation(
        b_receipt, config=migration.attestation_config
    )
    assert b_receipt["https_port"] == 7443
    assert b_receipt["runtime_publication"]["generation"] != (
        a_receipt["runtime_publication"]["generation"]
    )
    for relative, content in migration.b_source_bytes.items():
        assert b_receipt["files"][relative] == hashlib.sha256(content).hexdigest()
        assert b_receipt["files"][relative] != a_receipt["files"][relative]

    prior = b_receipt["tls_publication"]["prior"]
    assert prior is not None
    journal = layout.runtime / prior["journal"]
    retained_a_path = journal / "prior-build-receipt.json"
    assert retained_a_path.read_bytes() == migration.a_receipt_bytes
    retained_a = json.loads(retained_a_path.read_bytes())
    runtime_attestation.verify_runtime_build_attestation(
        retained_a, config=migration.attestation_config
    )
    assert retained_a["producer_attestation"] == a_receipt["producer_attestation"]
    for relative in migration.b_source_bytes:
        assert retained_a["files"][relative] == a_receipt["files"][relative]

    b_tls_identity = (layout.runtime / "tls").stat().st_ino
    b_tls_generation = b_receipt["tls_publication"]["generation"]
    retried = _run(layout)

    assert retried.returncode == 0, retried.stderr
    _assert_published(layout, B_LAN_IP)
    assert (layout.runtime / "tls").stat().st_ino == b_tls_identity
    retried_receipt = json.loads(receipt_path.read_bytes())
    assert retried_receipt["tls_publication"]["generation"] == b_tls_generation
    assert (journal / "prior-build-receipt.json").read_bytes() == migration.a_receipt_bytes
    assert stat.S_IMODE((journal / "prior-build-receipt.json").stat().st_mode) == 0o644
