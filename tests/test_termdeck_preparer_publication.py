from __future__ import annotations

import errno
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
                int(info.st_uid),
                int(info.st_gid),
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


def _legacy_seed_prefix(layout, length: int = 5) -> tuple[str, ...]:
    """Recreate native13's ordered copies, including the real five-leaf stop."""

    _config, producer, consumer = layout
    for relative, mode in publication.SOURCE_MODES.items():
        _write(producer / relative, (consumer / relative).read_bytes(), mode)
    _write(consumer / "tls" / "ca.srl", b"01\n", 0o644)
    names = sorted(publication.TLS_REQUIRED | publication.TLS_OPTIONAL)[:length]
    for name in names:
        _write(
            producer / "tls" / name, (consumer / "tls" / name).read_bytes(),
            (publication.TLS_REQUIRED_MODES | publication.TLS_OPTIONAL_MODES)[name],
        )
    return tuple(names)


def _seed_preserved_paths(producer: Path, consumer: Path) -> tuple[dict, dict]:
    # Directory mtime at the runtime root changes with staging. The retained
    # TLS directory and all preexisting leaf inodes/bytes/modes must survive.
    producer_paths = tuple(
        str(path.relative_to(producer))
        for path in producer.rglob("*") if path.is_file() or path == producer / "tls"
    )
    consumer_paths = tuple(
        str(path.relative_to(consumer)) for path in consumer.rglob("*") if path.is_file()
    )
    return _snapshot(producer, producer_paths), _snapshot(consumer, consumer_paths)


@pytest.mark.parametrize("bad_input", ("binary", "nginx", "extension", "missing-key", "recovery-key"))
def test_seed_preflights_every_source_before_any_copy(layout, bad_input: str) -> None:
    config, producer, consumer = layout
    if bad_input == "binary":
        (consumer / "bin" / "termdeck-web-server").chmod(0o777)
    elif bad_input == "nginx":
        (consumer / "nginx" / "nginx.conf").chmod(0o644)
    elif bad_input == "extension":
        (consumer / "tls" / "server.ext").chmod(0o644)
    elif bad_input == "missing-key":
        (consumer / "tls" / "server.key").unlink()
    else:
        _write(producer.parent / publication.CONTROL_KEY, b"different-private-key".ljust(32, b"!"), 0o600)
    before = _seed_preserved_paths(producer, consumer)

    with pytest.raises(publication.PublicationError):
        publication.seed(config)

    assert _seed_preserved_paths(producer, consumer) == before
    assert not tuple(producer.glob(".termdeck-*"))
    assert not (producer / "bin" / "termdeck-web-server").exists()
    assert not (producer / "nginx" / "nginx.conf").exists()
    assert not tuple((producer / "tls").iterdir())


@pytest.mark.parametrize("length", (1, 2, 3, 4, 5, 6))
def test_seed_resumes_only_the_intact_legacy_prefix_and_replays(layout, length: int) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout, length)
    (consumer / publication.CONTROL_KEY).unlink()
    private = _write(producer.parent / publication.CONTROL_KEY, b"private".ljust(32, b"!"), 0o600)
    before_private = _snapshot(producer.parent, (publication.CONTROL_KEY,))
    before_consumer = _seed_preserved_paths(producer, consumer)[1]
    before_sources = _snapshot(producer, tuple(publication.SOURCE_MODES))

    result = publication.seed(config)

    assert result["ok"] is True
    assert set(result["copied"]) == {"tls/" + name for name in publication.TLS_REQUIRED | publication.TLS_OPTIONAL}
    assert set(path.name for path in (producer / "tls").iterdir()) == publication.TLS_REQUIRED | publication.TLS_OPTIONAL
    for name, mode in (publication.TLS_REQUIRED_MODES | publication.TLS_OPTIONAL_MODES).items():
        destination = producer / "tls" / name
        assert destination.read_bytes() == (consumer / "tls" / name).read_bytes()
        info = destination.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink) == (
            config.preparer_uid, config.preparer_gid, mode, 1,
        )
    after = _seed_preserved_paths(producer, consumer)
    assert publication.seed(config)["copied"] == []
    assert _seed_preserved_paths(producer, consumer) == after
    assert after[1] == before_consumer
    assert _snapshot(producer, tuple(publication.SOURCE_MODES)) == before_sources
    assert _snapshot(producer.parent, (publication.CONTROL_KEY,)) == before_private
    assert private.read_bytes() == b"private".ljust(32, b"!")
    assert not tuple(producer.glob(".termdeck-*"))


@pytest.mark.parametrize(
    "invalid",
    ("gap", "different-bytes", "wrong-mode", "hardlink", "symlink", "extra-leaf",
     "missing-source", "changed-source", "receipt", "output", "journal", "nginx-output"),
)
def test_seed_refuses_partial_trees_without_exact_initial_seed_evidence(layout, invalid: str) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    if invalid == "gap":
        (producer / "tls" / "ca.srl").unlink()
    elif invalid == "different-bytes":
        (producer / "tls" / "ca.key").write_bytes(b"unrelated CA key")
    elif invalid == "wrong-mode":
        (producer / "tls" / "ca.key").chmod(0o644)
    elif invalid == "hardlink":
        os.link(producer / "tls" / "ca.key", producer.parent / "key-alias")
    elif invalid == "symlink":
        (producer / "tls" / "ca.key").unlink()
        (producer / "tls" / "ca.key").symlink_to(consumer / "tls" / "ca.key")
    elif invalid == "extra-leaf":
        _write(producer / "tls" / "unrelated", b"keep", 0o600)
    elif invalid == "missing-source":
        (producer / "bin" / "termdeck-web-server").unlink()
    elif invalid == "changed-source":
        (producer / "nginx" / "nginx.conf").write_bytes(b"different")
    elif invalid == "receipt":
        _write(producer / "build-receipt.json", b"{}", 0o644)
    elif invalid == "output":
        _write(producer / "current-lan-ip", b"192.168.1.1", 0o600)
    elif invalid == "journal":
        _mkdir(producer / (".termdeck-tls-transaction-" + GENERATION))
    else:
        _write(producer / "nginx" / "allowed-hosts.map", b"192.168.1.1 1;", 0o600)
    before = _seed_preserved_paths(producer, consumer)

    with pytest.raises(publication.PublicationError):
        publication.seed(config)

    assert _seed_preserved_paths(producer, consumer) == before
    assert not tuple(producer.glob(".termdeck-seed-*"))
    assert not (producer.parent / publication.CONTROL_KEY).exists()


def test_seed_preserves_a_complete_established_producer_generation(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer, b"established")
    before = _seed_preserved_paths(producer, consumer)

    result = publication.seed(config)

    assert result["copied"] == []
    assert _seed_preserved_paths(producer, consumer) == before


@pytest.mark.parametrize("partial", (False, True))
@pytest.mark.parametrize("fault", ("stage-write", "stage-sync", "exchange", "published-sync", "source-write", "key-migration"))
def test_tls_seed_faults_restore_exact_preimages_and_allow_replay(layout, monkeypatch, partial, fault) -> None:
    config, producer, consumer = layout
    if partial:
        _legacy_seed_prefix(layout)
    before = _seed_preserved_paths(producer, consumer)
    original_write = publication._write_all
    original_sync = publication.os.fsync
    original_rename = publication._rename_with_flags
    original_migrate = publication._migrate_control_key
    injected = False
    swapped = False

    def faulting_write(descriptor, content, *, label):
        nonlocal injected
        target = "staged producer TLS server.key" if fault == "stage-write" else "producer source bin/termdeck-web-server"
        if not injected and fault in {"stage-write", "source-write"} and label == target:
            injected = True
            raise publication.PublicationError("injected seed write failure")
        return original_write(descriptor, content, label=label)

    def faulting_sync(descriptor):
        nonlocal injected
        info = publication.os.fstat(descriptor)
        if not injected and stat.S_ISDIR(info.st_mode):
            target = producer if fault == "published-sync" and swapped else None
            if (target is not None and info.st_ino == target.stat().st_ino) or (
                fault == "stage-sync" and "server.key" in os.listdir(descriptor)
                and info.st_ino != (producer / "tls").stat().st_ino
            ):
                injected = True
                raise OSError(errno.EIO, "injected seed sync failure")
        return original_sync(descriptor)

    def faulting_rename(*args, **kwargs):
        nonlocal injected, swapped
        if kwargs.get("label") == "initial producer TLS":
            if fault == "exchange" and not injected:
                injected = True
                raise publication.PublicationError("injected seed exchange failure")
            result = original_rename(*args, **kwargs)
            swapped = True
            return result
        return original_rename(*args, **kwargs)

    def faulting_migrate(*args, **kwargs):
        nonlocal injected
        if fault == "key-migration":
            injected = True
            raise publication.PublicationError("injected key migration failure")
        return original_migrate(*args, **kwargs)

    # A resumed prefix retains matching source inodes; fail its subsequent
    # source read instead of manufacturing an unnecessary replacement.
    original_optional = publication._optional_leaf

    def faulting_optional(*args, **kwargs):
        nonlocal injected
        if fault == "source-write" and partial and swapped and not injected and kwargs.get("label") == "producer source bin/termdeck-web-server":
            injected = True
            raise publication.PublicationError("injected source inspection failure")
        return original_optional(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write_all", faulting_write)
        patch.setattr(publication.os, "fsync", faulting_sync)
        patch.setattr(publication, "_rename_with_flags", faulting_rename)
        patch.setattr(publication, "_migrate_control_key", faulting_migrate)
        patch.setattr(publication, "_optional_leaf", faulting_optional)
        with pytest.raises(publication.PublicationError):
            publication.seed(config)

    assert injected
    assert _seed_preserved_paths(producer, consumer) == before
    assert not tuple(producer.glob(".termdeck-seed-*"))
    assert not (producer.parent / publication.CONTROL_KEY).exists()
    assert publication.seed(config)["ok"] is True


@pytest.mark.parametrize("existing_directory", (False, True))
def test_initial_tls_is_visible_only_as_an_entire_set(layout, monkeypatch, existing_directory) -> None:
    config, producer, consumer = layout
    if not existing_directory:
        (producer / "tls").rmdir()
    original = publication._write_all
    stages = []

    def observe_stage(descriptor, content, *, label):
        if label.startswith("staged producer TLS "):
            stages.append(label)
            assert (producer / "tls").exists() is existing_directory
            if existing_directory:
                assert not tuple((producer / "tls").iterdir())
        return original(descriptor, content, label=label)

    monkeypatch.setattr(publication, "_write_all", observe_stage)
    assert publication.seed(config)["ok"] is True
    assert len(stages) == len(publication.TLS_REQUIRED)
    assert {p.name for p in (producer / "tls").iterdir()} == publication.TLS_REQUIRED


@pytest.mark.parametrize("changed", ("consumer-tls", "consumer-source", "producer-prefix", "generation"))
def test_seed_revalidates_sources_prefix_and_generation_after_staging(layout, monkeypatch, changed) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    original = publication._copy_into_noreplace
    before = _seed_preserved_paths(producer, consumer)
    injected = False
    drifted = None

    def mutate_after_staging(*args, **kwargs):
        nonlocal injected, drifted
        result = original(*args, **kwargs)
        if kwargs.get("label") == "staged producer TLS server.key" and not injected:
            injected = True
            if changed == "consumer-tls":
                drifted = consumer / "tls" / "server.key"
            elif changed == "consumer-source":
                drifted = consumer / "bin" / "termdeck-web-server"
            elif changed == "producer-prefix":
                drifted = producer / "tls" / "ca.key"
            else:
                drifted = producer / "build-receipt.json"
            drifted.write_bytes(b"concurrent-change")
        return result

    monkeypatch.setattr(publication, "_copy_into_noreplace", mutate_after_staging)
    with pytest.raises(publication.PublicationError, match="changed|generation"):
        publication.seed(config)

    assert injected and drifted.read_bytes() == b"concurrent-change"
    assert not (producer / "tls" / "server.key").exists()
    assert not (producer.parent / publication.CONTROL_KEY).exists()
    assert (consumer / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert not tuple(producer.glob(".termdeck-seed-*"))
    # None of the unaffected preimages may be rewritten by this rejection.
    after = _seed_preserved_paths(producer, consumer)
    for old, new, root in zip(before, after, (producer, consumer)):
        for relative, entry in old.items():
            if root / relative != drifted and relative != "tls":
                assert new[relative] == entry


def test_seed_exchange_race_restores_the_displaced_conflicting_generation(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    before = _seed_preserved_paths(producer, consumer)
    original = publication._rename_with_flags
    injected = False
    concurrent = None

    def replace_before_exchange(*args, **kwargs):
        nonlocal injected, concurrent
        if kwargs.get("label") == "initial producer TLS" and not injected:
            injected = True
            (producer / "tls").rename(producer.parent / "retained-original-tls")
            _mkdir(producer / "tls")
            for name, mode in publication.TLS_REQUIRED_MODES.items():
                _write(producer / "tls" / name, b"concurrent-" + name.encode(), mode)
            concurrent = _snapshot(producer, ("tls",) + tuple("tls/" + name for name in publication.TLS_REQUIRED))
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "_rename_with_flags", replace_before_exchange)
    with pytest.raises(publication.PublicationError, match="changed"):
        publication.seed(config)

    assert injected
    assert _snapshot(producer, tuple(concurrent)) == concurrent
    original_tree = producer.parent / "retained-original-tls"
    for relative, entry in before[0].items():
        if relative.startswith("tls/"):
            name = relative.removeprefix("tls/")
            assert _snapshot(original_tree, (name,))[name] == entry
    assert _seed_preserved_paths(producer, consumer)[1] == before[1]
    assert not tuple(producer.glob(".termdeck-seed-*"))


def test_seed_never_overwrites_a_concurrent_initial_tls_destination(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    (producer / "tls").rmdir()
    original = publication._rename_noreplace
    concurrent = None

    def create_before_publish(*args, **kwargs):
        nonlocal concurrent
        if kwargs.get("label") == "initial producer TLS":
            _mkdir(producer / "tls")
            _write(producer / "tls" / "ca.key", b"concurrent-private-key", 0o600)
            concurrent = _snapshot(producer, ("tls", "tls/ca.key"))
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "_rename_noreplace", create_before_publish)
    with pytest.raises(publication.PublicationError):
        publication.seed(config)

    assert _snapshot(producer, tuple(concurrent)) == concurrent
    assert (consumer / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert not (producer.parent / publication.CONTROL_KEY).exists()
    assert not tuple(producer.glob(".termdeck-seed-*"))


def test_seed_preserves_both_preimage_and_conflict_if_rollback_path_is_replaced(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    before = _snapshot(producer, ("tls", "tls/ca.key"))
    original_rename = publication._rename_with_flags
    original_sync = publication.os.fsync
    swapped = False
    injected = False
    concurrent = None

    def observe_exchange(*args, **kwargs):
        nonlocal swapped
        result = original_rename(*args, **kwargs)
        if kwargs.get("label") == "initial producer TLS":
            swapped = True
        return result

    def replace_and_fail(descriptor):
        nonlocal injected, concurrent
        if swapped and not injected and os.fstat(descriptor).st_ino == producer.stat().st_ino:
            injected = True
            (producer / "tls").rename(producer.parent / "displaced-new-seed")
            _mkdir(producer / "tls")
            _write(producer / "tls" / "ca.key", b"concurrent-private-key", 0o600)
            concurrent = _snapshot(producer, ("tls", "tls/ca.key"))
            raise OSError(errno.EIO, "injected post-publication race")
        return original_sync(descriptor)

    monkeypatch.setattr(publication, "_rename_with_flags", observe_exchange)
    monkeypatch.setattr(publication.os, "fsync", replace_and_fail)
    with pytest.raises(publication.PublicationError):
        publication.seed(config)

    assert injected and _snapshot(producer, tuple(concurrent)) == concurrent
    retained, = producer.glob(".termdeck-seed-tls-*")
    preserved = _snapshot(producer, (retained.name, retained.name + "/ca.key"))
    assert preserved[retained.name] == before["tls"]
    assert preserved[retained.name + "/ca.key"] == before["tls/ca.key"]
    assert (consumer / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert not (producer.parent / publication.CONTROL_KEY).exists()


def test_seed_stage_open_failure_cleans_empty_candidate_without_descriptor_leak(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    before = _seed_preserved_paths(producer, consumer)
    descriptor_count = len(os.listdir("/proc/self/fd"))
    original = publication.os.open

    def fail_stage_open(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".termdeck-seed-tls-"):
            raise OSError(errno.EMFILE, "injected seed stage open failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(publication.os, "open", fail_stage_open)
    with pytest.raises(publication.PublicationError):
        publication.seed(config)

    assert _seed_preserved_paths(producer, consumer) == before
    assert not tuple(producer.glob(".termdeck-seed-*"))
    assert len(os.listdir("/proc/self/fd")) == descriptor_count


@pytest.mark.parametrize("private_exists", (False, True))
@pytest.mark.parametrize("fault", ("retained-sync", "consumer-sync", "commit-unlink"))
def test_seed_key_migration_failure_preserves_exact_legacy_and_private_inodes(layout, monkeypatch, private_exists, fault) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    if private_exists:
        _write(producer.parent / publication.CONTROL_KEY, b"k" * 32, 0o600)
    before = _seed_preserved_paths(producer, consumer)
    private_before = _snapshot(producer.parent, (publication.CONTROL_KEY,)) if private_exists else None
    original_sync = publication.os.fsync
    original_unlink = publication.os.unlink
    injected = False

    def faulting_sync(descriptor):
        nonlocal injected
        info = os.fstat(descriptor)
        is_directory = stat.S_ISDIR(info.st_mode)
        retained = not (consumer / publication.CONTROL_KEY).exists()
        if not injected and retained and is_directory and (
            (fault == "consumer-sync" and info.st_ino == consumer.stat().st_ino)
            or (fault == "retained-sync" and os.listdir(descriptor) == [publication.CONTROL_KEY])
        ):
            injected = True
            raise OSError(errno.EIO, "injected retained key sync failure")
        return original_sync(descriptor)

    def faulting_unlink(path, *args, **kwargs):
        nonlocal injected
        if fault == "commit-unlink" and path == publication.CONTROL_KEY and not injected:
            injected = True
            raise OSError(errno.EIO, "injected key commit failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "fsync", faulting_sync)
        patch.setattr(publication.os, "unlink", faulting_unlink)
        with pytest.raises(publication.PublicationError):
            publication.seed(config)

    assert injected
    assert _seed_preserved_paths(producer, consumer) == before
    assert not tuple(consumer.glob(".termdeck-key-migration-*"))
    assert not tuple(producer.glob(".termdeck-seed-*"))
    if private_exists:
        assert _snapshot(producer.parent, (publication.CONTROL_KEY,)) == private_before
    else:
        assert not (producer.parent / publication.CONTROL_KEY).exists()
    assert publication.seed(config)["ok"] is True


def test_seed_postcommit_tls_cleanup_failure_is_success_with_retained_preimage(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    before = _snapshot(producer, ("tls", "tls/ca.key"))
    original = publication._remove_flat_directory

    def fail_tls_cleanup(parent, name, **kwargs):
        if name.startswith(".termdeck-seed-tls-"):
            raise OSError(errno.EIO, "injected TLS cleanup failure")
        return original(parent, name, **kwargs)

    monkeypatch.setattr(publication, "_remove_flat_directory", fail_tls_cleanup)
    result = publication.seed(config)

    assert result["ok"] is True
    assert result["cleanup_warnings"]
    assert {p.name for p in (producer / "tls").iterdir()} == publication.TLS_REQUIRED | publication.TLS_OPTIONAL
    retained, = producer.glob(".termdeck-seed-tls-*")
    retained_snapshot = _snapshot(producer, (retained.name, retained.name + "/ca.key"))
    assert retained_snapshot[retained.name] == before["tls"]
    assert retained_snapshot[retained.name + "/ca.key"] == before["tls/ca.key"]
    assert publication.seed(config)["ok"] is True


def test_seed_postcommit_key_cleanup_failure_keeps_committed_tls_and_private_key(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    _legacy_seed_prefix(layout)
    original_unlink = publication.os.unlink
    original_sync = publication.os.fsync
    committed_key_stage = None
    injected = False

    def observe_key_commit(path, *args, **kwargs):
        nonlocal committed_key_stage
        result = original_unlink(path, *args, **kwargs)
        if path == publication.CONTROL_KEY:
            committed_key_stage = kwargs["dir_fd"]
        return result

    def fail_committed_sync(descriptor):
        nonlocal injected
        if descriptor == committed_key_stage and not injected:
            injected = True
            raise OSError(errno.EIO, "injected committed key directory sync failure")
        return original_sync(descriptor)

    monkeypatch.setattr(publication.os, "unlink", observe_key_commit)
    monkeypatch.setattr(publication.os, "fsync", fail_committed_sync)
    result = publication.seed(config)

    assert injected and result["ok"] is True and result["cleanup_warnings"]
    assert result["control_key"] == "migrated"
    assert not (consumer / publication.CONTROL_KEY).exists()
    assert (producer.parent / publication.CONTROL_KEY).read_bytes() == b"k" * 32
    assert {p.name for p in (producer / "tls").iterdir()} == publication.TLS_REQUIRED | publication.TLS_OPTIONAL
    retained_stage, = consumer.glob(".termdeck-key-migration-*")
    assert not tuple(retained_stage.iterdir())
    assert stat.S_IMODE(retained_stage.stat().st_mode) == 0o700


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

    from termdeck.service_wrappers import _validate_preparer_publication_result

    assert _validate_preparer_publication_result(result) is None

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


@pytest.mark.parametrize(
    ("phase", "failure"),
    [
        (phase, failure)
        for phase in (
            "receipt-retain",
            "tls-retain",
            "tls-publish",
            "lan-retain",
            "lan-publish",
            "hosts-retain",
            "hosts-publish",
            "receipt-publish",
        )
        for failure in ("rename", "fsync")
        if (phase, failure) != ("tls-retain", "fsync")
    ],
)
def test_publication_rename_and_sync_failures_restore_consumer_preimages(
    layout,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    tracked = (
        "build-receipt.json",
        "tls",
        *("tls/" + name for name in sorted(publication.TLS_REQUIRED)),
        "current-lan-ip",
        "nginx/allowed-hosts.map",
        "state/sessions.db",
        "state/auth.json",
        "releases/keep/artifact",
    )
    before = _snapshot(consumer, tracked)
    before_names = {
        "runtime": set(os.listdir(consumer)),
        "nginx": set(os.listdir(consumer / "nginx")),
    }
    original_rename = publication.os.rename
    original_fsync = publication.os.fsync
    pending_sync: int | None = None
    injected = False

    def rename_phase(source: str, destination: str) -> str | None:
        targets = {
            "build-receipt.json": "receipt",
            "current-lan-ip": "lan",
            "allowed-hosts.map": "hosts",
        }
        if source == "tls" and destination.startswith(".termdeck-prior-dir-"):
            return "tls-retain"
        if source.startswith(".termdeck-new-tls-") and destination == "tls":
            return "tls-publish"
        if source in targets and destination.startswith(".termdeck-prior-"):
            return targets[source] + "-retain"
        if source.startswith(".termdeck-new-") and destination in targets:
            return targets[destination] + "-publish"
        return None

    def faulting_rename(source, destination, *args, **kwargs):
        nonlocal pending_sync, injected
        selected = rename_phase(os.fsdecode(source), os.fsdecode(destination)) == phase
        if selected and not injected and failure == "rename":
            injected = True
            raise OSError(errno.EIO, "injected publication rename failure")
        result = original_rename(source, destination, *args, **kwargs)
        if selected and not injected and failure == "fsync":
            pending_sync = kwargs["dst_dir_fd"]
        return result

    def faulting_fsync(descriptor):
        nonlocal pending_sync, injected
        if pending_sync == descriptor and not injected:
            pending_sync = None
            injected = True
            raise OSError(errno.EIO, "injected publication directory sync failure")
        return original_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "rename", faulting_rename)
        patch.setattr(publication.os, "fsync", faulting_fsync)
        with pytest.raises(publication.PublicationError):
            publication.publish(config)

    assert injected is True
    assert _snapshot(consumer, tracked) == before
    assert set(os.listdir(consumer)) == before_names["runtime"]
    assert set(os.listdir(consumer / "nginx")) == before_names["nginx"]


@pytest.mark.parametrize("operation_number", (1, 2), ids=("receipt", "tls"))
@pytest.mark.parametrize("failure", ("dup", "set_inheritable"))
def test_transaction_descriptor_failure_preserves_preimages_and_closes_descriptors(
    layout,
    monkeypatch: pytest.MonkeyPatch,
    operation_number: int,
    failure: str,
) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    tracked = (
        "build-receipt.json",
        "tls",
        *("tls/" + name for name in sorted(publication.TLS_REQUIRED)),
        "current-lan-ip",
        "nginx/allowed-hosts.map",
    )
    before = _snapshot(consumer, tracked)
    before_names = set(os.listdir(consumer))
    original_dup = publication.os.dup
    original_set_inheritable = publication.os.set_inheritable
    duplicated: list[int] = []
    attempts = 0
    injected = False

    def faulting_dup(descriptor):
        nonlocal attempts, injected
        attempts += 1
        if attempts == operation_number and failure == "dup":
            injected = True
            raise OSError(errno.EMFILE, "injected transaction descriptor exhaustion")
        retained = original_dup(descriptor)
        duplicated.append(retained)
        return retained

    def faulting_set_inheritable(descriptor, inheritable):
        nonlocal injected
        if attempts == operation_number and failure == "set_inheritable":
            injected = True
            raise OSError(errno.EIO, "injected descriptor setup failure")
        return original_set_inheritable(descriptor, inheritable)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "dup", faulting_dup)
        patch.setattr(publication.os, "set_inheritable", faulting_set_inheritable)
        with pytest.raises(publication.PublicationError, match="transaction descriptor"):
            publication.publish(config)

    assert injected is True
    assert _snapshot(consumer, tracked) == before
    assert set(os.listdir(consumer)) == before_names
    for descriptor in duplicated:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


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
        if destination != publication.CONTROL_KEY:
            return original(
                source_parent, source, destination_parent, destination, label=label,
            )
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


def _published_tls_snapshot(consumer: Path) -> dict[str, object]:
    return _snapshot(
        consumer,
        ("tls",) + tuple(
            "tls/" + path.name for path in sorted((consumer / "tls").iterdir())
        ),
    )


def test_publish_reuses_exact_consumer_tls_without_staging(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    before = _published_tls_snapshot(consumer)

    def forbid_tls_stage(*_args, **_kwargs):
        raise AssertionError("an exact consumer TLS copy must retain its inodes")

    monkeypatch.setattr(publication, "_stage_tls", forbid_tls_stage)
    result = publication.publish(config)

    assert _published_tls_snapshot(consumer) == before
    assert result["tls"]["identity"][1] != (producer / "tls").stat().st_ino
    assert publication.verify(config)["tls"] == result["tls"]


def test_publish_reuses_tls_across_a_fresh_runtime_receipt(layout) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    before = _published_tls_snapshot(consumer)
    _write(producer / "nginx" / "allowed-hosts.map", b"192.168.50.9 1;\nlocalhost 1;\n", 0o600)
    receipt = _receipt(producer)
    receipt["runtime_publication"]["generation"] = "1" * 32
    _write(producer / "build-receipt.json", _canonical(receipt), 0o644)

    result = publication.publish(config)

    assert result["generation"] == "1" * 32
    assert _published_tls_snapshot(consumer) == before
    assert (consumer / "nginx" / "allowed-hosts.map").read_bytes().endswith(b"localhost 1;\n")
    assert publication.verify(config)["tls"] == result["tls"]


def test_publish_reuses_optional_tls_serial_inode(layout) -> None:
    config, producer, consumer = layout
    _write(consumer / "tls" / "ca.srl", b"1000\n", 0o644)
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    before = _published_tls_snapshot(consumer)

    publication.publish(config)

    assert _published_tls_snapshot(consumer) == before
    assert (consumer / "tls" / "ca.srl").read_bytes() == b"1000\n"


@pytest.mark.parametrize("fault", ("mode", "hard-link", "symlink", "unapproved-leaf"))
def test_publish_refuses_unsafe_existing_tls_before_consumer_writes(layout, fault: str) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    key = consumer / "tls" / "server.key"
    if fault == "mode":
        key.chmod(0o644)
    elif fault == "hard-link":
        os.link(key, consumer.parent / "owned-key-link")
    elif fault == "symlink":
        retained = consumer.parent / "owned-retained-key"
        key.rename(retained)
        key.symlink_to(retained)
    else:
        _write(consumer / "tls" / "unrelated", b"owned fixture evidence", 0o600)
    generated = _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    )
    before = (consumer / "tls").stat().st_ino

    with pytest.raises(publication.PublicationError):
        publication.publish(config)

    assert (consumer / "tls").stat().st_ino == before
    assert _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    ) == generated


@pytest.mark.parametrize("replacement", ("leaf", "directory", "unapproved-leaf"))
def test_publish_tls_reuse_refuses_and_preserves_concurrent_writer(
    layout, monkeypatch, replacement: str,
) -> None:
    import shutil

    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    generated = _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    )
    original = publication.Transaction.replace_file
    changed = False
    foreign = None

    def concurrent_write(self, *args, label: str, **kwargs):
        nonlocal changed, foreign
        if label == "consumer output current-lan-ip" and not changed:
            changed = True
            if replacement == "directory":
                retained = consumer.parent / "owned-retained-tls"
                (consumer / "tls").rename(retained)
                shutil.copytree(retained, consumer / "tls")
            elif replacement == "leaf":
                key = consumer / "tls" / "server.key"
                key.rename(consumer.parent / "owned-retained-key")
                _write(key, b"concurrent owned fixture key", 0o600)
            else:
                _write(consumer / "tls" / "unrelated", b"concurrent owned evidence", 0o600)
            foreign = _published_tls_snapshot(consumer)
        return original(self, *args, label=label, **kwargs)

    monkeypatch.setattr(publication.Transaction, "replace_file", concurrent_write)
    with pytest.raises(publication.PublicationError, match="TLS.*changed"):
        publication.publish(config)

    assert changed
    assert foreign == _published_tls_snapshot(consumer)
    assert _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    ) == generated


def test_publish_failure_during_tls_reuse_retains_exact_tls_preimages(layout, monkeypatch) -> None:
    config, producer, consumer = layout
    publication.seed(config)
    _prepare_generation(producer)
    publication.publish(config)
    before = _published_tls_snapshot(consumer)
    generated = _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    )
    original = publication.Transaction.replace_file

    def fail_after_reuse(self, *args, label: str, **kwargs):
        if label == "consumer output nginx/allowed-hosts.map":
            raise publication.PublicationError("owned reuse fault")
        return original(self, *args, label=label, **kwargs)

    monkeypatch.setattr(publication.Transaction, "replace_file", fail_after_reuse)
    with pytest.raises(publication.PublicationError, match="owned reuse fault"):
        publication.publish(config)

    assert _published_tls_snapshot(consumer) == before
    assert _snapshot(
        consumer, ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map")
    ) == generated
