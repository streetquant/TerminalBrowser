from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "termdeck-preparer-publication"
GENERATION = "0123456789abcdef0123456789abcdef"


def _load_helper():
    name = "termdeck_preparer_publication_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(HELPER_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


publication = _load_helper()


def _mkdir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _write(path: Path, content: bytes, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _identity(path: Path) -> list[int]:
    info = path.stat(follow_symlinks=False)
    return [
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    ]


def _claim(path: Path, relative: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "identity": _identity(path),
        "mode": stat.S_IMODE(path.stat().st_mode),
        "path": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _snapshot(root: Path, relatives: tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for relative in relatives:
        path = root / relative
        info = path.lstat()
        entry: dict[str, object] = {
            "identity": (
                int(info.st_dev),
                int(info.st_ino),
                stat.S_IMODE(info.st_mode),
                int(info.st_nlink),
                int(info.st_size),
                int(info.st_mtime_ns),
            ),
            "kind": "directory" if path.is_dir() else "file",
        }
        if path.is_file():
            entry["content"] = path.read_bytes()
        result[relative] = entry
    return result


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _receipt(producer: Path) -> dict[str, object]:
    source_files = {
        "bin/termdeck-web-server": producer / "bin" / "termdeck-web-server",
        "nginx/nginx.conf": producer / "nginx" / "nginx.conf",
        "tls/ca.crt": producer / "tls" / "ca.crt",
        "tls/server.crt": producer / "tls" / "server.crt",
        "tls/server-chain.crt": producer / "tls" / "server-chain.crt",
    }
    files = {
        relative: hashlib.sha256(path.read_bytes()).hexdigest()
        for relative, path in source_files.items()
    }
    outputs = {
        relative: _claim(producer / relative, relative)
        for relative in publication.OUTPUT_MODES
    }
    tls_manifest = {
        path.name: {
            key: value
            for key, value in _claim(path, path.name).items()
            if key != "path"
        }
        for path in sorted((producer / "tls").iterdir())
        if path.name in publication.TLS_REQUIRED | publication.TLS_OPTIONAL
    }
    tls_info = (producer / "tls").stat()
    return {
        "files": files,
        "https_port": 7443,
        "lan_ip": "192.168.50.9",
        "ok": True,
        "producer_attestation": {"fixture": True},
        "producer_commitment": {"fixture": True},
        "runtime_publication": {
            "generation": GENERATION,
            "outputs": outputs,
        },
        "schema": "termdeck.lan-runtime-build.v2",
        "tls_publication": {
            "generation": GENERATION,
            "current": {
                "path": "tls",
                "identity": [
                    int(tls_info.st_dev),
                    int(tls_info.st_ino),
                    stat.S_IMODE(tls_info.st_mode),
                    int(tls_info.st_uid),
                ],
                "manifest": tls_manifest,
            },
            "prior": None,
        },
    }


def _prepare_generation(producer: Path, marker: bytes = b"new") -> dict[str, object]:
    tls_modes = {
        "ca.crt": 0o644,
        "ca.key": 0o600,
        "server.key": 0o600,
        "server.crt": 0o644,
        "server-chain.crt": 0o644,
        "server.ext": 0o600,
    }
    for name, mode in tls_modes.items():
        _write(producer / "tls" / name, marker + b"-tls-" + name.encode("ascii"), mode)
    _write(producer / "current-lan-ip", b"192.168.50.9\n", 0o600)
    _write(
        producer / "nginx" / "allowed-hosts.map",
        b"192.168.50.9 1;\n",
        0o600,
    )
    receipt = _receipt(producer)
    _write(producer / "build-receipt.json", _canonical(receipt), 0o644)
    return receipt


@pytest.fixture
def layout(tmp_path: Path):
    uid = os.getuid()
    gid = os.getgid()
    producer_parent = _mkdir(tmp_path / "producer")
    producer = _mkdir(producer_parent / "runtime")
    consumer = _mkdir(tmp_path / "consumer")
    for root in (producer, consumer):
        _mkdir(root / "bin")
        _mkdir(root / "nginx")
        _mkdir(root / "tls")

    _write(consumer / "bin" / "termdeck-web-server", b"consumer-binary-v1", 0o755)
    _write(consumer / "nginx" / "nginx.conf", b"consumer-nginx-v1", 0o600)
    _write(consumer / "current-lan-ip", b"192.168.1.10\n", 0o600)
    _write(consumer / "nginx" / "allowed-hosts.map", b"192.168.1.10 1;\n", 0o600)
    _write(
        consumer / "build-receipt.json",
        _canonical({"schema": "termdeck.lan-runtime-build.v1", "ok": True}),
        0o644,
    )
    old_tls_modes = {
        "ca.crt": 0o644,
        "ca.key": 0o600,
        "server.key": 0o600,
        "server.crt": 0o644,
        "server-chain.crt": 0o644,
        "server.ext": 0o600,
    }
    for name, mode in old_tls_modes.items():
        _write(consumer / "tls" / name, b"old-tls-" + name.encode("ascii"), mode)
    _write(consumer / publication.CONTROL_KEY, b"k" * 32, 0o600)
    _write(consumer / "state" / "sessions.db", b"session-state", 0o600)
    _write(consumer / "state" / "auth.json", b"auth-state", 0o600)
    _write(consumer / "releases" / "keep" / "artifact", b"release-state", 0o600)

    config = publication.Config(
        producer,
        consumer,
        uid,
        gid,
        uid,
        gid,
        enforce_principal_separation=False,
    )
    return config, producer, consumer


def test_config_requires_distinct_production_principals(tmp_path: Path) -> None:
    producer = _mkdir(tmp_path / "producer" / "runtime")
    consumer = _mkdir(tmp_path / "consumer")
    with pytest.raises(publication.PublicationError, match="must be separated"):
        publication.Config(
            producer,
            consumer,
            os.getuid(),
            os.getgid(),
            os.getuid(),
            os.getgid(),
        )


def test_seed_copies_only_approved_inputs_tls_and_recovery_key(layout) -> None:
    config, producer, consumer = layout
    result = publication.seed(config)

    assert result["ok"] is True
    assert (producer / "bin" / "termdeck-web-server").read_bytes() == b"consumer-binary-v1"
    assert (producer / "nginx" / "nginx.conf").read_bytes() == b"consumer-nginx-v1"
    assert (producer.parent / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert {path.name for path in (producer / "tls").iterdir()} == publication.TLS_REQUIRED
    assert not (producer / "state").exists()
    assert not (producer / "releases").exists()
    assert (consumer / "state" / "sessions.db").read_bytes() == b"session-state"
    assert (consumer / "state" / "auth.json").read_bytes() == b"auth-state"
    assert (consumer / "releases" / "keep" / "artifact").read_bytes() == b"release-state"


def test_publish_and_verify_bind_consumer_postimages_without_touching_user_state(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    receipt = _prepare_generation(producer)
    unrelated = _snapshot(
        consumer,
        ("state/sessions.db", "state/auth.json", "releases/keep/artifact"),
    )

    result = publication.publish(config)
    observed = publication.verify(config)

    assert result["ok"] is True
    assert result["operation"] == "publish"
    assert result["generation"] == GENERATION
    assert observed["generation"] == GENERATION
    assert (consumer / "build-receipt.json").read_bytes() == _canonical(receipt)
    assert (consumer / "current-lan-ip").read_bytes() == b"192.168.50.9\n"
    assert (consumer / "nginx" / "allowed-hosts.map").read_bytes() == b"192.168.50.9 1;\n"
    assert (consumer / "tls" / "server.key").read_bytes() == b"new-tls-server.key"
    assert result["receipt"]["identity"] == _identity(consumer / "build-receipt.json")
    assert result["outputs"]["current-lan-ip"]["identity"] == _identity(
        consumer / "current-lan-ip"
    )
    assert result["outputs"]["current-lan-ip"]["identity"] != _identity(
        producer / "current-lan-ip"
    )
    assert _snapshot(
        consumer,
        ("state/sessions.db", "state/auth.json", "releases/keep/artifact"),
    ) == unrelated


def test_tampered_producer_output_is_rejected_before_consumer_mutation(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    before = _snapshot(
        consumer,
        (
            "build-receipt.json",
            "tls",
            "tls/server.key",
            "current-lan-ip",
            "nginx/allowed-hosts.map",
            "state/sessions.db",
        ),
    )
    _write(producer / "current-lan-ip", b"203.0.113.7\n", 0o600)

    with pytest.raises(publication.PublicationError, match="does not match receipt"):
        publication.publish(config)

    assert _snapshot(
        consumer,
        (
            "build-receipt.json",
            "tls",
            "tls/server.key",
            "current-lan-ip",
            "nginx/allowed-hosts.map",
            "state/sessions.db",
        ),
    ) == before


def test_mid_publication_failure_restores_exact_consumer_preimages(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    tracked = (
        "build-receipt.json",
        "tls",
        "tls/ca.crt",
        "tls/ca.key",
        "tls/server.key",
        "tls/server.crt",
        "tls/server-chain.crt",
        "tls/server.ext",
        "current-lan-ip",
        "nginx/allowed-hosts.map",
        "state/sessions.db",
        "state/auth.json",
    )
    before = _snapshot(consumer, tracked)
    original = publication.Transaction.replace_file

    def fail_on_hosts(self, *args, label: str, **kwargs):
        if label == "consumer output nginx/allowed-hosts.map":
            raise publication.PublicationError("injected publication failure")
        return original(self, *args, label=label, **kwargs)

    monkeypatch.setattr(publication.Transaction, "replace_file", fail_on_hosts)
    with pytest.raises(publication.PublicationError, match="injected publication failure"):
        publication.publish(config)

    assert _snapshot(consumer, tracked) == before
    temporary_prefixes = (
        ".termdeck-new-",
        ".termdeck-prior-",
        ".termdeck-prior-dir-",
        ".termdeck-rollback-dir-",
    )
    assert not any(path.name.startswith(temporary_prefixes) for path in consumer.iterdir())
    assert not any(
        path.name.startswith(temporary_prefixes) for path in (consumer / "nginx").iterdir()
    )


def test_symlink_output_is_rejected_and_earlier_swaps_roll_back(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    target = consumer / "state" / "sessions.db"
    (consumer / "current-lan-ip").unlink()
    (consumer / "current-lan-ip").symlink_to(target)
    before_receipt = _snapshot(consumer, ("build-receipt.json", "tls", "tls/server.key"))

    with pytest.raises(publication.PublicationError, match="prior consumer output"):
        publication.publish(config)

    assert (consumer / "current-lan-ip").is_symlink()
    assert (consumer / "current-lan-ip").readlink() == target
    assert _snapshot(consumer, ("build-receipt.json", "tls", "tls/server.key")) == before_receipt


def test_verify_rejects_consumer_tamper(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    _write(consumer / "nginx" / "allowed-hosts.map", b"tampered\n", 0o600)

    with pytest.raises(publication.PublicationError, match="differs from producer"):
        publication.verify(config)


def test_seed_reports_absent_when_neither_recovery_key_exists(layout) -> None:
    config, producer, consumer = layout
    (consumer / publication.CONTROL_KEY).unlink()

    result = publication.seed(config)

    assert result["control_key"] == "absent"
    assert not (consumer / publication.CONTROL_KEY).exists()
    assert not (producer.parent / publication.CONTROL_KEY).exists()


def test_seed_preserves_existing_private_recovery_key_without_legacy(layout) -> None:
    config, producer, consumer = layout
    (consumer / publication.CONTROL_KEY).unlink()
    private = _write(
        producer.parent / publication.CONTROL_KEY,
        b"p" * 32,
        0o600,
    )
    before = _identity(private)

    result = publication.seed(config)

    assert result["control_key"] == "private"
    assert private.read_bytes() == b"p" * 32
    assert _identity(private) == before
    assert not (consumer / publication.CONTROL_KEY).exists()


def test_seed_removes_matching_legacy_key_without_rewriting_private_key(layout) -> None:
    config, producer, consumer = layout
    private = _write(
        producer.parent / publication.CONTROL_KEY,
        b"k" * 32,
        0o600,
    )
    before = _identity(private)

    result = publication.seed(config)

    assert result["control_key"] == "migrated"
    assert private.read_bytes() == b"k" * 32
    assert _identity(private) == before
    assert not (consumer / publication.CONTROL_KEY).exists()


def test_seed_rejects_mismatched_private_and_legacy_recovery_keys(layout) -> None:
    config, producer, consumer = layout
    private = _write(
        producer.parent / publication.CONTROL_KEY,
        b"p" * 32,
        0o600,
    )

    with pytest.raises(publication.PublicationError, match="do not match"):
        publication.seed(config)

    assert private.read_bytes() == b"p" * 32
    assert (consumer / publication.CONTROL_KEY).read_bytes() == b"k" * 32


def test_seed_rejects_hardlinked_legacy_recovery_key(layout) -> None:
    config, producer, consumer = layout
    legacy = consumer / publication.CONTROL_KEY
    alias = consumer / "state" / "legacy-key-alias"
    os.link(legacy, alias)

    with pytest.raises(publication.PublicationError, match="bounded regular file"):
        publication.seed(config)

    assert legacy.samefile(alias)
    assert legacy.read_bytes() == b"k" * 32
    assert not (producer.parent / publication.CONTROL_KEY).exists()


def test_seed_rejects_concurrent_private_key_destination_without_replacement(
    layout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, producer, consumer = layout
    original = publication._rename_noreplace

    def create_destination_then_rename(
        source_parent: int,
        source: str,
        destination_parent: int,
        destination: str,
        *,
        label: str,
    ) -> None:
        descriptor = os.open(
            destination,
            publication.WRITE_FLAGS,
            0o600,
            dir_fd=destination_parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, config.preparer_uid, config.preparer_gid)
            os.write(descriptor, b"r" * 32)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        original(
            source_parent,
            source,
            destination_parent,
            destination,
            label=label,
        )

    monkeypatch.setattr(publication, "_rename_noreplace", create_destination_then_rename)
    with pytest.raises(publication.PublicationError, match="appeared during no-replace"):
        publication.seed(config)

    assert (consumer / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert (producer.parent / publication.CONTROL_KEY).read_bytes() == b"r" * 32


def test_publish_rejects_wrong_server_extension_mode_before_consumer_mutation(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    (producer / "tls" / "server.ext").chmod(0o644)
    tracked = (
        "build-receipt.json",
        "tls",
        "tls/server.ext",
        "current-lan-ip",
        "nginx/allowed-hosts.map",
        "state/sessions.db",
    )
    before = _snapshot(consumer, tracked)

    with pytest.raises(publication.PublicationError, match="server.ext ownership, mode"):
        publication.publish(config)

    assert _snapshot(consumer, tracked) == before


def test_postcommit_backup_cleanup_failure_is_a_warning_not_false_rollback(
    layout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    receipt = _prepare_generation(producer)
    original_unlink = publication.os.unlink
    injected = False

    def fail_one_retained_file(path, *args, **kwargs):
        nonlocal injected
        name = os.fsdecode(path)
        if (
            not injected
            and name.startswith(".termdeck-prior-")
            and not name.startswith(".termdeck-prior-dir-")
        ):
            injected = True
            raise OSError("injected retained-preimage cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "unlink", fail_one_retained_file)
        result = publication.publish(config)

    assert injected is True
    assert result["ok"] is True
    assert result["operation"] == "publish"
    assert result["cleanup_warnings"]
    assert (consumer / "build-receipt.json").read_bytes() == _canonical(receipt)
    assert (consumer / "current-lan-ip").read_bytes() == b"192.168.50.9\n"
    assert (consumer / "state" / "sessions.db").read_bytes() == b"session-state"

    retained = [
        path
        for path in consumer.rglob(".termdeck-prior-*")
        if not path.name.startswith(".termdeck-prior-dir-")
    ]
    assert len(retained) == 1
    retained[0].unlink()
