"""Bounded, source-owned TermDeck web activation transaction."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from . import release_manager
from . import runtime_attestation
from .release_manager import build_release, install_release, rollback_release, verify_installed_tree

# The web activation transaction owns only the backend and LAN service
# lifecycle.  The complete six-unit publication and process identity policy is
# kept in ``service_wrappers`` so the unit templates, activation path, and
# generated managed files share one command contract.
from . import service_wrappers

SCHEMA = "termdeck.web-activation.v2"
PUBLICATION_INTENT_SCHEMA = "termdeck.web-activation-publication-intent.v2"
RUNTIME_PUBLICATION_SCHEMA = "termdeck.lan-runtime-build.v2"
RELEASE_PUBLICATION_SCHEMA = "termdeck.web-activation-release-publication.v1"
WATCHDOG_UNIT = "termdeck-web-recovery.service"
WATCHDOG_TIMEOUT = 120.0
SERVICES = ("termdeck-web-backend.service", "termdeck-web-lan.service")
# Activation receipts observe stable TermDeck runtime processes only. Model and
# client executables remain opaque and are handled by session metadata.
PROCESS_OBSERVATION_NAMES = ("tmux", "t1000")
GENERATED_RUNTIME = (
    "current-lan-ip",
    "nginx/allowed-hosts.map",
    "build-receipt.json",
)
RUNTIME_PREPARER_OUTPUTS = GENERATED_RUNTIME[:2]
RUNTIME_PREPARER_SOURCE_FILES = {
    "bin/termdeck-web-server",
    "nginx/nginx.conf",
    "tls/ca.crt",
    "tls/server-chain.crt",
    "tls/server.crt",
}
RUNTIME_PREPARER_SOURCE_PATHS = {
    "bin/termdeck-web-server": "bin/termdeck-web-server",
    "nginx/nginx.conf": "nginx/nginx.conf",
    "tls/ca.crt": "tls/ca.crt",
    "tls/server-chain.crt": "tls/server-chain.crt",
    "tls/server.crt": "tls/server.crt",
}
RUNTIME_TLS_CURRENT_PATH = "tls"
RUNTIME_TLS_PRIOR_PATH = "prior-tls"
RUNTIME_TLS_JOURNAL_PREFIX = ".termdeck-tls-transaction-"
RUNTIME_TLS_REQUIRED_MODES = {
    "server.key": 0o600,
    "server.crt": 0o644,
    "server-chain.crt": 0o644,
    "server.ext": 0o600,
}
RUNTIME_TLS_REQUIRED_LEAVES = frozenset({"ca.crt", "ca.key", *RUNTIME_TLS_REQUIRED_MODES})
RUNTIME_TLS_ALLOWED_LEAVES = RUNTIME_TLS_REQUIRED_LEAVES | {"ca.srl"}
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
# Keep the retirement guard independent of the retired disk prefix.
# The stable suffix still rejects any old source-runtime bind marker.
OBSOLETE_RUNTIME_MARKER = b"termdeck/.runtime"
TERMINAL = {"rolled-back", "rollback-failed"}
MUTATION_PHASES = {
    "mutation-start", "watchdog-installing", "watchdog-armed",
    "package-installing", "package-installed", "runtime-publishing",
    "runtime-staged", "services-stopping", "services-stopped",
    "services-starting", "activated", "failed", "rollback-start",
    "rollback-failed",
}


class WebActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivationPaths:
    target: Path
    evidence: Path
    runtime: Path
    database: Path
    managed_root: Path
    managed_bin: Path
    unit_dir: Path

    @classmethod
    def defaults(cls, **overrides: Path | None) -> "ActivationPaths":
        source = Path(overrides.get("target") or Path(__file__).resolve().parents[1]).resolve()
        state_root = Path(
            os.environ.get(
                "TERMDECK_STATE_ROOT",
                str(service_wrappers.CANONICAL_STATE_ROOT),
            )
        ).expanduser()
        if not state_root.is_absolute():
            raise WebActivationError("TERMDECK_STATE_ROOT must be an absolute path")
        database = Path(
            overrides.get("database") or state_root / "sessions.sqlite3"
        ).expanduser()
        if not database.is_absolute():
            raise WebActivationError("activation database must be an absolute path")
        evidence = Path(
            overrides.get("evidence")
            or state_root / "recovery" / "activation-v2"
        ).expanduser()
        if not evidence.is_absolute():
            raise WebActivationError("activation evidence must be an absolute path")
        return cls(
            source,
            evidence.resolve(),
            Path(
                overrides.get("runtime") or service_wrappers.CANONICAL_RUNTIME_ROOT
            ).resolve(),
            database.resolve(),
            Path(
                overrides.get("managed_root") or service_wrappers.CANONICAL_MANAGED_ROOT
            ).resolve(),
            Path(
                overrides.get("managed_bin") or service_wrappers.CANONICAL_MANAGED_BIN
            ).resolve(),
            Path(overrides.get("unit_dir") or "/home/arch/.config/systemd/user").resolve(),
        )

    @property
    def latest(self) -> Path:
        return self.evidence / "LATEST"

    @property
    def recovery_required(self) -> Path:
        return self.evidence / "RECOVERY_REQUIRED"


_LOCK = threading.RLock()
_WATCHDOG_EXECUTION = threading.local()


@contextmanager
def _watchdog_recovery_context():
    """Keep a watchdog's inverse alive until its terminal receipt is durable."""

    previous = getattr(_WATCHDOG_EXECUTION, "recovering", False)
    _WATCHDOG_EXECUTION.recovering = True
    try:
        yield
    finally:
        _WATCHDOG_EXECUTION.recovering = previous


@contextmanager
def _activation_lock(paths: ActivationPaths):
    paths.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = paths.evidence / "activation.lock"
    with lock_path.open("a+", encoding="ascii") as stream:
        os.chmod(lock_path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


@contextmanager
def _try_activation_lock(paths: ActivationPaths):
    """Try the activation lock without making the watchdog block activation."""

    paths.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = paths.evidence / "activation.lock"
    with lock_path.open("a+", encoding="ascii") as stream:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_DIRECTORY_IDENTITY_FIELDS = ("device", "inode", "mode")
_FILE_IDENTITY_FIELDS = (
    "device", "inode", "mode", "nlink", "size", "mtime_ns", "ctime_ns",
)


def _publication_path(path: Path | str, *, label: str) -> Path:
    try:
        return release_manager._canonical_publication_path(path, label=label)
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc


def _stored_publication_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise WebActivationError(f"{label} is not a path")
    return _publication_path(Path(value), label=label)


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WebActivationError(f"invalid {label} in activation receipt")
    return value


def _directory_identity_payload(identity: object) -> dict[str, int]:
    if not isinstance(identity, release_manager._DirectoryIdentity):
        raise WebActivationError("invalid publication parent identity")
    return {field: int(getattr(identity, field)) for field in _DIRECTORY_IDENTITY_FIELDS}


def _directory_identity_from_payload(value: object) -> release_manager._DirectoryIdentity:
    if not isinstance(value, Mapping) or set(value) != set(_DIRECTORY_IDENTITY_FIELDS):
        raise WebActivationError("invalid publication parent identity in activation receipt")
    device = _integer(value["device"], label="publication parent device")
    inode = _integer(value["inode"], label="publication parent inode", minimum=1)
    mode = _integer(value["mode"], label="publication parent mode")
    return release_manager._DirectoryIdentity(device, inode, mode)


def _file_identity_payload(identity: object | None) -> dict[str, int] | None:
    if identity is None:
        return None
    if not isinstance(identity, release_manager._FileIdentity):
        raise WebActivationError("invalid activation file identity")
    return {field: int(getattr(identity, field)) for field in _FILE_IDENTITY_FIELDS}


def _file_identity_from_payload(value: object) -> release_manager._FileIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(_FILE_IDENTITY_FIELDS):
        raise WebActivationError("invalid activation file identity in receipt")
    values = {
        field: _integer(value[field], label=f"activation file {field}")
        for field in _FILE_IDENTITY_FIELDS
    }
    if values["inode"] <= 0 or values["nlink"] != 1:
        raise WebActivationError("invalid activation file identity in receipt")
    return release_manager._FileIdentity(**values)


def _capture_parent_guards(
    paths: Sequence[Path], *, create_parents: bool = False,
) -> release_manager._PublicationParentGuards:
    try:
        return release_manager._capture_publication_parent_guards(
            tuple(paths), create_parents=create_parents,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc


def _parent_guard_payload(parent_guards: object, path: Path) -> dict[str, object]:
    if not isinstance(parent_guards, release_manager._PublicationParentGuards):
        raise WebActivationError("invalid publication parent guards")
    guard = parent_guards.for_path(path)
    if guard is None:
        raise WebActivationError(f"publication parent is unavailable: {path.parent}")
    return {
        "path": str(guard.path),
        "chain": [_directory_identity_payload(identity) for identity in guard.chain],
    }


def _parent_guards_from_payload(
    value: object, path: Path,
) -> release_manager._PublicationParentGuards:
    if not isinstance(value, Mapping) or set(value) != {"path", "chain"}:
        raise WebActivationError("invalid publication parent guard in activation receipt")
    parent = _stored_publication_path(value["path"], label="publication parent guard")
    if parent != path.parent:
        raise WebActivationError("activation receipt parent guard does not match target")
    raw_chain = value["chain"]
    if not isinstance(raw_chain, list) or not raw_chain:
        raise WebActivationError("invalid publication parent guard chain in activation receipt")
    chain = tuple(_directory_identity_from_payload(item) for item in raw_chain)
    guard = release_manager._DirectoryGuard(parent, chain)
    return release_manager._PublicationParentGuards((guard,))


def _release_publication_parent_guards_from_payload(
    value: object,
) -> release_manager._PublicationParentGuards:
    """Rebuild every descriptor anchor retained by a release callback.

    A combined release-manager publication can span wrappers, activation
    state, and a first-install receipt.  Its durable callback hand-off holds
    the complete anchor set rather than one leaf's parent guard.  Reconstruct
    it before inspecting a retained journal so a parent replacement cannot
    redirect recovery toward a same-named foreign journal.
    """

    if not isinstance(value, list) or not value:
        raise WebActivationError("managed release publication parents are invalid")
    guards: list[release_manager._DirectoryGuard] = []
    seen: set[Path] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "chain"}:
            raise WebActivationError("managed release publication parent is invalid")
        parent = _stored_publication_path(
            item.get("path"), label="managed release publication parent"
        )
        raw_chain = item.get("chain")
        if not isinstance(raw_chain, list) or not raw_chain:
            raise WebActivationError("managed release publication parent chain is invalid")
        if parent in seen:
            raise WebActivationError("managed release publication repeats a parent")
        seen.add(parent)
        guards.append(
            release_manager._DirectoryGuard(
                parent,
                tuple(_directory_identity_from_payload(entry) for entry in raw_chain),
            )
        )
    return release_manager._PublicationParentGuards(tuple(guards))


def _capture_anchored(
    path: Path,
    *,
    label: str,
    parent_guards: object | None = None,
    create_parents: bool = False,
) -> tuple[Path, release_manager._AnchoredFileSnapshot]:
    candidate = _publication_path(path, label=label)
    try:
        captures = release_manager._capture_anchored_files_safely(
            ((candidate, label),),
            parent_guards=parent_guards,
            create_parents=create_parents,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc
    return candidate, captures[candidate]


def _fingerprint_from_capture(
    path: Path, capture: release_manager._AnchoredFileSnapshot,
) -> dict[str, object]:
    if not isinstance(capture, release_manager._AnchoredFileSnapshot):
        raise WebActivationError("invalid anchored activation file capture")
    snapshot = capture.snapshot
    if not snapshot.exists:
        if capture.identity is not None:
            raise WebActivationError("invalid absent activation file capture")
        return {"path": str(path), "exists": False}
    if capture.identity is None:
        raise WebActivationError("activation file identity is unavailable")
    return {
        "path": str(path),
        "exists": True,
        "sha256": hashlib.sha256(snapshot.content).hexdigest(),
        "size": len(snapshot.content),
        "mode": snapshot.mode,
    }


def _postimage_payload(
    path: Path,
    capture: release_manager._AnchoredFileSnapshot,
    parent_guards: release_manager._PublicationParentGuards,
) -> dict[str, object]:
    record = _fingerprint_from_capture(path, capture)
    if not record.get("exists"):
        raise WebActivationError(f"publication did not create expected activation file: {path}")
    if not isinstance(capture, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    return {
        "sha256": record["sha256"],
        "size": record["size"],
        "mode": record["mode"],
        "identity": _file_identity_payload(capture.identity),
        "parent_guard": _parent_guard_payload(parent_guards, path),
    }


def _expected_postimage(
    path: Path,
    capture: release_manager._AnchoredFileSnapshot,
    value: object,
) -> release_manager._AnchoredFileSnapshot:
    if not isinstance(capture, release_manager._AnchoredFileSnapshot):
        raise WebActivationError("invalid anchored activation file capture")
    expected_fields = {"sha256", "size", "mode", "identity", "parent_guard"}
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise WebActivationError("invalid activation postimage in receipt")
    if not capture.snapshot.exists or capture.identity is None:
        raise WebActivationError(f"activation target disappeared before rollback: {path}")
    expected_hash = value["sha256"]
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise WebActivationError("invalid activation postimage digest in receipt")
    if hashlib.sha256(capture.snapshot.content).hexdigest() != expected_hash:
        raise WebActivationError(f"activation target changed before rollback: {path}")
    if _integer(value["size"], label="activation postimage size") != len(capture.snapshot.content):
        raise WebActivationError(f"activation target changed before rollback: {path}")
    if _integer(value["mode"], label="activation postimage mode") != capture.snapshot.mode:
        raise WebActivationError(f"activation target changed before rollback: {path}")
    expected_identity = _file_identity_from_payload(value["identity"])
    if expected_identity is None or capture.identity != expected_identity:
        raise WebActivationError(f"activation target changed before rollback: {path}")
    _parent_guards_from_payload(value["parent_guard"], path)
    return release_manager._AnchoredFileSnapshot(capture.snapshot, expected_identity, expected_hash)


def _publish_anchored(
    path: Path,
    content: bytes | None,
    mode: int,
    *,
    label: str,
    parent_guards: release_manager._PublicationParentGuards,
    expected_preimage: release_manager._AnchoredFileSnapshot,
    journal_path: Path | None = None,
    intent_checkpoint: Callable[[release_manager.PublicationTransactionReceipt], None] | None = None,
    postimage_checkpoint: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object] | None:
    published: dict[str, object] | None = None

    def before_publish(receipt: release_manager.PublicationTransactionReceipt) -> None:
        if receipt.state != "intent" or (
            journal_path is not None and receipt.journal_path != journal_path
        ):
            raise WebActivationError("activation publication intent hand-off is invalid")
        if receipt.parent_guards != parent_guards:
            raise WebActivationError("activation publication intent parent guard changed")
        if receipt.preimages.get(path) != expected_preimage or receipt.postimages:
            raise WebActivationError("activation publication intent preimage changed")
        if intent_checkpoint is not None:
            intent_checkpoint(receipt)

    def after_publish(receipt: release_manager.PublicationTransactionReceipt) -> None:
        nonlocal published
        if receipt.state != "published" or (
            journal_path is not None and receipt.journal_path != journal_path
        ):
            raise WebActivationError("activation publication postimage hand-off is invalid")
        if receipt.parent_guards != parent_guards or receipt.preimages.get(path) != expected_preimage:
            raise WebActivationError("activation publication postimage preimage changed")
        observed = receipt.postimages.get(path)
        if not isinstance(observed, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("activation publication has no anchored postimage")
        if content is None:
            if observed.snapshot.exists or observed.identity is not None:
                raise WebActivationError(f"activation deletion was not applied: {path}")
            return
        if (
            not observed.snapshot.exists
            or observed.snapshot.content != content
            or observed.snapshot.mode != mode
            or observed.identity is None
        ):
            raise WebActivationError(f"activation publication did not verify: {path}")
        # This callback runs while release_manager still holds its durable
        # publication journal.  Persisting the outer receipt here closes the
        # otherwise fatal return-to-caller gap: a process crash can recover
        # the exact B inode from the named inner journal instead of adopting a
        # same-content foreign leaf.
        published = _postimage_payload(path, observed, parent_guards)
        if postimage_checkpoint is not None:
            postimage_checkpoint(published)

    try:
        # The release-manager primitive persists an intent before any public
        # rename.  Callers that retain a named journal in their own receipt
        # can recover an interruption before ``verify`` runs; callers that
        # receive ``postimage_checkpoint`` durably bind B during ``verify``.
        release_manager._publish_files_transactionally(
            ((path, content, mode, label),),
            parent_guards=parent_guards,
            expected_preimages={path: expected_preimage},
            journal_path=journal_path,
            before_publish=before_publish if journal_path is not None else None,
            after_publish=after_publish,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc
    if content is None:
        return None
    if published is None:
        raise WebActivationError(f"activation publication did not verify: {path}")
    return published


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> dict[str, object]:
    candidate = _publication_path(path, label="activation destination")
    parent_guards = _capture_parent_guards((candidate,), create_parents=True)
    _path, before = _capture_anchored(
        candidate,
        label="activation destination",
        parent_guards=parent_guards,
    )
    return _publish_anchored(
        candidate,
        data,
        mode,
        label="activation destination",
        parent_guards=parent_guards,
        expected_preimage=before,
    ) or {}


def _write(path: Path, value: object, mode: int = 0o600) -> None:
    _atomic(path, (json.dumps(value, sort_keys=True, indent=2, default=str) + "\n").encode(), mode)


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WebActivationError(f"invalid activation receipt: {path}") from exc
    if not isinstance(value, dict):
        raise WebActivationError(f"activation receipt is not an object: {path}")
    return value


def _run(argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(list(argv), cwd=cwd, env=dict(env) if env else None,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = exc.strerror if isinstance(exc, OSError) else "timeout"
        raise WebActivationError(f"command could not complete: {argv[0]} ({detail})") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise WebActivationError(f"command failed: {Path(argv[0]).name}: {detail[-1] if detail else result.returncode}")
    return result


def _read_regular(
    path: Path, *, label: str,
) -> tuple[Path, release_manager._AnchoredFileSnapshot]:
    candidate, capture = _capture_anchored(path, label=label)
    if not isinstance(capture, release_manager._AnchoredFileSnapshot) or not capture.snapshot.exists:
        raise WebActivationError(f"activation path does not exist: {candidate}")
    return candidate, capture


def _sha(path: Path) -> str:
    _path, capture = _read_regular(path, label="activation digest source")
    if not isinstance(capture, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    return hashlib.sha256(capture.snapshot.content).hexdigest()


def _fingerprint(path: Path) -> dict[str, object]:
    candidate, capture = _capture_anchored(path, label="activation fingerprint")
    return _fingerprint_from_capture(candidate, capture)


def _source(paths: ActivationPaths) -> dict[str, str]:
    def git(*args: str, check: bool = True) -> str:
        return _run(["/usr/bin/git", *args], cwd=paths.target, timeout=30, check=check).stdout.strip()
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise WebActivationError("TermDeck source is dirty; commit the qualified source first")
    result = _run(["/usr/bin/git", "rev-parse", "origin/main"], cwd=paths.target, timeout=30, check=False)
    return {"commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "branch": git("branch", "--show-current"), "origin_main": result.stdout.strip()}


def _persist(paths: ActivationPaths, tx: Path, receipt: dict[str, object], status: str | None = None) -> None:
    with _LOCK:
        old = receipt.get("status")
        if status is not None:
            receipt["status"] = status
        receipt["updated_at"] = _now()
        receipt["heartbeat_epoch"] = time.time()
        history = receipt.setdefault("phase_history", [])
        if isinstance(history, list) and (not history or history[-1] != receipt["status"]):
            history.append(receipt["status"])
        if receipt.get("status") in MUTATION_PHASES and receipt.get("status") != "failed":
            receipt["mutation_started"] = True
        _write(tx / "receipt.json", receipt)


def _checkpoint_publication(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    record: str,
) -> None:
    """Durably journal one externally visible transaction publication.

    The anchored publisher has already fsynced its file and parent directory
    when this runs.  Persisting the receipt before another external boundary
    makes that postimage recoverable after a process crash rather than keeping
    it only in the in-memory receipt object.
    """

    if not isinstance(record, str) or not record:
        raise WebActivationError("activation publication journal record is invalid")
    # The heartbeat shares this receipt object.  Keep the journal append and
    # its fsynced receipt replacement in one RLock-protected critical section
    # so a heartbeat cannot publish a partially updated in-memory receipt
    # between the external file commit and this durable checkpoint.
    with _LOCK:
        journal = receipt.setdefault("publication_journal", [])
        if not isinstance(journal, list):
            raise WebActivationError("activation publication journal is invalid")
        journal.append({"sequence": len(journal) + 1, "record": record, "at": _now()})
        _persist(paths, tx, receipt)


def new_transaction(paths: ActivationPaths, source: Mapping[str, str] | None = None) -> tuple[Path, dict[str, object]]:
    paths.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.evidence, 0o700)
    tx = paths.evidence / ("tx-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(5).hex())
    tx.mkdir(mode=0o700)
    identity = dict(source or _source(paths))
    receipt: dict[str, object] = {
        "schema": SCHEMA, "tx_id": tx.name, "created_at": _now(),
        "status": "prepared", "mutation_started": False, "target": str(paths.target),
        "runtime": str(paths.runtime), "database": str(paths.database),
        "managed_root": str(paths.managed_root), "managed_bin": str(paths.managed_bin),
        "unit_dir": str(paths.unit_dir), "services": list(SERVICES),
        "source": identity, "package": {}, "phase_history": ["prepared"],
    }
    _write(tx / "receipt.json", receipt)
    _write(paths.latest, {"schema": SCHEMA, "transaction": tx.name, "source": identity})
    return tx, receipt


def load_transaction(paths: ActivationPaths, tx: Path) -> tuple[Path, dict[str, object]]:
    tx = tx.resolve()
    if tx.parent != paths.evidence.resolve():
        raise WebActivationError("transaction is outside the activation evidence root")
    receipt = _read(tx / "receipt.json")
    if receipt.get("schema") != SCHEMA or receipt.get("target") != str(paths.target):
        raise WebActivationError("transaction identity does not match the source checkout")
    status = receipt.get("status")
    if status in {"prepared", "artifacts-staged", "activated", *TERMINAL}:
        return tx, receipt
    if receipt.get("mutation_started") and status in MUTATION_PHASES:
        return tx, receipt
    raise WebActivationError(f"transaction is not actionable: {status!r}")


def latest_transaction(paths: ActivationPaths) -> Path:
    value = _read(paths.latest).get("transaction")
    if not isinstance(value, str) or Path(value).name != value:
        raise WebActivationError("LATEST transaction pointer is invalid")
    return paths.evidence / value


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise WebActivationError(f"session database is unavailable: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_database(paths: ActivationPaths, tx: Path, label: str) -> dict[str, object]:
    destination = tx / f"database-{label}.sqlite3"
    source = _ro(paths.database)
    try:
        output = sqlite3.connect(str(destination), timeout=2)
        try:
            source.backup(output)
            output.commit()
        except sqlite3.Error as exc:
            raise WebActivationError("supported SQLite live backup failed") from exc
        finally:
            output.close()
        ids = [str(row[0]) for row in source.execute("SELECT id FROM sessions ORDER BY id")]
    finally:
        source.close()
    check = _ro(destination)
    try:
        integrity = [str(row[0]) for row in check.execute("PRAGMA integrity_check")]
    finally:
        check.close()
    if integrity != ["ok"]:
        raise WebActivationError(f"SQLite integrity check failed: {destination}")
    value = {"path": str(destination), "integrity": "ok", "session_ids": ids, "session_count": len(ids)}
    _write(tx / f"database-{label}.json", value)
    return value


def verify_database(paths: ActivationPaths, tx: Path, before: Mapping[str, object]) -> dict[str, object]:
    after = snapshot_database(paths, tx, "after")
    old = {str(x) for x in before.get("session_ids", []) if isinstance(x, str)}
    new = {str(x) for x in after.get("session_ids", []) if isinstance(x, str)}
    lost = sorted(old - new)
    if lost:
        raise WebActivationError("pre-existing session rows disappeared: " + ",".join(lost))
    return {"before_count": len(old), "after_count": len(new),
            "preserved_session_ids": len(old) - len(lost), "new_session_ids": len(new - old),
            "lost_session_ids": lost, "integrity": "ok", "wal_shm_byte_identity_required": False}


def process_snapshot() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name in PROCESS_OBSERVATION_NAMES:
        result = _run(["/usr/bin/pgrep", "-x", name], timeout=10, check=False)
        if result.returncode not in (0, 1):
            raise WebActivationError(f"process observation failed for {name}")
        for raw in result.stdout.splitlines():
            if not raw.isdigit():
                continue
            try:
                text = Path(f"/proc/{int(raw)}/stat").read_text()
            except OSError:
                continue
            close = text.rfind(") ")
            fields = text[close + 2:].split() if close >= 0 else []
            if len(fields) >= 20:
                rows.append({"pid": int(raw), "start_ticks": int(fields[19]), "comm": name})
    return rows


def process_observation(before: object) -> dict[str, object]:
    old = {(str(x["comm"]), int(x["pid"]), int(x["start_ticks"])) for x in before if isinstance(x, dict)} if isinstance(before, list) else set()
    now = process_snapshot()
    current = {(str(x["comm"]), int(x["pid"]), int(x["start_ticks"])) for x in now}
    disappeared = [{"comm": a, "pid": b, "start_ticks": c} for a, b, c in sorted(old - current)]
    return {
        "before": len(old),
        "after": len(current),
        "disappeared": disappeared,
        "activation_signal_scope": list(SERVICES),
        "natural_exit_allowed": True,
        "protected_process_kill_evidence": {
            "allowlisted_units_only": True,
            "disappeared_processes_observed": disappeared,
            "conclusion": "no activation signal was sent outside the two web units",
        },
    }


def _wrapper_runtime_root(paths: ActivationPaths) -> Path:
    """Return the runtime root consumed by the managed-wrapper API.

    Production ``ActivationPaths.runtime`` is the canonical ``.termdeck``
    directory itself.  Older disposable activation fixtures may still pass a
    nested ``<custom-root>/termdeck-lan`` path; retain that isolated shape
    without allowing an arbitrary production parent to redirect the wrapper.
    """

    if paths.runtime == service_wrappers.CANONICAL_RUNTIME_ROOT:
        return paths.runtime
    if paths.runtime.name != "termdeck-lan":
        raise WebActivationError(
            "activation runtime must be the canonical .termdeck directory or an exact "
            "<custom-root>/termdeck-lan directory"
        )
    return paths.runtime.parent


def _wrapper_kwargs(paths: ActivationPaths) -> dict[str, object]:
    return {
        "source_root": paths.target,
        "runtime_root": _wrapper_runtime_root(paths),
        "state_root": paths.database.parent,
        "managed_root": paths.managed_root,
        "managed_bin": paths.managed_bin,
        "unit_dir": paths.unit_dir,
    }


def _release_wrapper_kwargs(
    paths: ActivationPaths,
    release_id: str,
) -> dict[str, object]:
    """Return the immutable wrapper-execution binding for ``release_id``.

    The recovery wrapper is a Python program.  Its bytes must not import the
    mutable checkout after the release which rendered it has been selected.
    Ask the release manager to re-verify the frozen release site on every
    preflight, then pass that exact module binding to the wrapper API.  The
    returned layout also records the root-owned attestation control namespace;
    activation treats it as metadata and never tries to read a preparer key or
    connect to the broker as the untrusted runtime user.
    """

    if not isinstance(release_id, str) or not release_id:
        raise WebActivationError("managed wrapper execution release id is invalid")
    try:
        result = release_manager.release_wrapper_execution_binding(
            root=paths.managed_root,
            bin_dir=paths.managed_bin,
            release_id=release_id,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(
            f"managed wrapper execution binding failed: {exc}"
        ) from exc
    if not isinstance(result, Mapping):
        raise WebActivationError("managed wrapper execution binding is invalid")
    if result.get("release_id") != release_id or result.get("immutable") is not True:
        raise WebActivationError("managed wrapper execution is not immutable")
    execution_root = result.get("execution_root")
    execution_binding = result.get("execution_binding")
    control_root = result.get("control_root")
    control_socket = result.get("control_socket")
    control_attestation = result.get("control_attestation")
    if (
        not isinstance(execution_root, (str, Path))
        or not isinstance(execution_binding, Mapping)
        or not isinstance(control_root, (str, Path))
        or not isinstance(control_socket, str)
        or not isinstance(control_attestation, Mapping)
    ):
        raise WebActivationError("managed wrapper execution binding is invalid")
    try:
        layout = service_wrappers.resolve_layout(
            **_wrapper_kwargs(paths),
            execution_root=execution_root,
            execution_binding=execution_binding,
            control_root=control_root,
        )
    except service_wrappers.WrapperError as exc:
        raise WebActivationError(
            f"managed wrapper execution binding is invalid: {exc}"
        ) from exc
    expected_socket = str(
        layout.control_root / service_wrappers.CONTROL_ATTEST_SOCKET_NAME
    )
    if control_socket != expected_socket:
        raise WebActivationError("managed wrapper control socket is invalid")
    expected_attestation = {
        "socket": expected_socket,
        "operation": service_wrappers.CONTROL_ATTEST_OPERATION,
        "request_schema": service_wrappers.CONTROL_ATTEST_REQUEST_SCHEMA,
        "response_schema": service_wrappers.CONTROL_ATTEST_RESPONSE_SCHEMA,
        "producer_principal": service_wrappers.CONTROL_ATTEST_PRODUCER,
        "excluded_principal": service_wrappers.CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
        "control_root_owner": service_wrappers.CONTROL_ATTEST_OWNER,
        "control_root_group": service_wrappers.CONTROL_ATTEST_GROUP,
        "control_root_mode": service_wrappers.CONTROL_ROOT_MODE,
        "socket_owner": service_wrappers.CONTROL_ATTEST_OWNER,
        "socket_group": service_wrappers.CONTROL_ATTEST_GROUP,
        "socket_mode": service_wrappers.CONTROL_ATTEST_SOCKET_MODE,
    }
    if dict(control_attestation) != expected_attestation:
        raise WebActivationError("managed wrapper control attestation is invalid")
    normalized_binding = {
        name: {"sha256": digest, "size": size}
        for name, digest, size in layout.execution_binding
    }
    if not normalized_binding:
        raise WebActivationError("managed wrapper execution binding is empty")
    return {
        **_wrapper_kwargs(paths),
        "execution_root": layout.execution_root,
        "execution_binding": normalized_binding,
        "control_root": layout.control_root,
    }


def _request_runtime_preparation(
    paths: ActivationPaths,
    *,
    restore_mode: str,
    generation: str | None = None,
) -> Mapping[str, object]:
    """Request the producer inverse through the appropriate testable boundary.

    The installed production runtime is private to ``termdeck-preparer`` and
    must be reached through its fixed broker.  Focused activation fixtures use
    disposable runtime roots, where no broker is allowed to serve the custom
    tree; those roots retain the copied wrapper command as their explicit
    producer boundary.
    """

    if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
        try:
            result = service_wrappers._request_lan_preparation(
                restore_mode=restore_mode,
                generation=generation,
                runtime_root=paths.runtime,
                timeout=90.0,
            )
        except service_wrappers.WrapperError as exc:
            raise WebActivationError("LAN runtime TLS rollback broker request failed") from exc
        result = _unwrap_broker_inverse(result)
    else:
        if restore_mode == "generation":
            if not isinstance(generation, str):
                raise WebActivationError("LAN runtime restore generation is missing")
            arguments = [
                str(paths.runtime / "bin" / "termdeck-web-prepare"),
                "--restore-tls-generation",
                generation,
            ]
        elif restore_mode == "unreceipted":
            arguments = [
                str(paths.runtime / "bin" / "termdeck-web-prepare"),
                "--restore-unreceipted-tls-generation",
            ]
        else:
            raise WebActivationError("LAN runtime restore mode is invalid")
        try:
            completed = _run(
                arguments,
                env={"TERMDECK_RUNTIME_ROOT": str(paths.runtime)},
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebActivationError("LAN runtime TLS rollback producer request failed") from exc
        try:
            result = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WebActivationError("LAN runtime TLS rollback producer returned invalid JSON") from exc

    if not isinstance(result, Mapping):
        raise WebActivationError("LAN runtime TLS rollback producer returned no attested result")
    return result


def _unwrap_broker_inverse(value: object) -> dict[str, object]:
    """Keep producer inverse authority separate from consumer copy identities."""

    if (
        not isinstance(value, Mapping) or set(value) != {"producer", "publication"}
        or not isinstance(value["producer"], Mapping)
        or not isinstance(value["publication"], Mapping)
    ):
        raise WebActivationError("LAN runtime inverse broker envelope is invalid")
    producer = dict(value["producer"])
    consumer = dict(value["publication"])
    has_warnings = "consumer_cleanup_warnings" in consumer
    warnings = consumer.pop("consumer_cleanup_warnings", None)
    if has_warnings and (
        not isinstance(warnings, list) or len(warnings) > 8
        or any(not isinstance(item, str) or not item.strip() or len(item) > 512 for item in warnings)
        or len(runtime_attestation._canonical_json(warnings)) > 4096
    ):
        raise WebActivationError("LAN runtime inverse cleanup warnings are invalid")
    if producer.get("action") == "restored-runtime-generation":
        try:
            for relative in GENERATED_RUNTIME:
                original = producer["receipt"] if relative == "build-receipt.json" else producer["restored"]["outputs"][relative]
                copied = consumer["receipt"] if relative == "build-receipt.json" else consumer["restored"]["outputs"][relative]
                mode = 0o644 if relative == "build-receipt.json" else 0o600
                if (
                    not isinstance(copied, Mapping)
                    or set(copied) != {"path", "sha256", "mode", "identity"}
                    or copied["path"] != relative or type(copied["mode"]) is not int
                    or copied["mode"] != mode or copied["sha256"] != original["sha256"]
                ):
                    raise WebActivationError("LAN runtime inverse consumer claim is invalid")
                identity = _runtime_receipt_identity(copied["identity"], label="inverse consumer")
                source = _runtime_receipt_identity(original["identity"], label="inverse producer")
                if identity.mode != mode or identity.nlink != 1 or identity.size != source.size:
                    raise WebActivationError("LAN runtime inverse consumer content differs from producer")
            consumer["receipt"] = producer["receipt"]
            consumer["restored"] = {**consumer["restored"], "outputs": producer["restored"]["outputs"]}
        except (KeyError, TypeError) as exc:
            raise WebActivationError("LAN runtime inverse broker claims are invalid") from exc
    elif producer.get("action") != "restored-tls-generation":
        raise WebActivationError("LAN runtime inverse broker action is invalid")
    if consumer != producer:
        raise WebActivationError("LAN runtime inverse consumer lineage differs from producer")
    return {**producer, "consumer_publication": dict(value["publication"])}


def _wrapper_execution_identity(
    paths: ActivationPaths,
    release_id: str,
) -> dict[str, object]:
    """Serialize the selected immutable execution context for a receipt."""

    kwargs = _release_wrapper_kwargs(paths, release_id)
    execution_root = kwargs.get("execution_root")
    execution_binding = kwargs.get("execution_binding")
    control_root = kwargs.get("control_root")
    if (
        not isinstance(execution_root, Path)
        or not isinstance(execution_binding, Mapping)
        or not isinstance(control_root, Path)
    ):
        raise WebActivationError("managed wrapper execution binding is invalid")
    return {
        "execution_root": str(execution_root),
        "execution_binding": dict(execution_binding),
        "control_root": str(control_root),
        "control_socket": str(
            control_root / service_wrappers.CONTROL_ATTEST_SOCKET_NAME
        ),
        "control_attestation": {
            "socket": str(control_root / service_wrappers.CONTROL_ATTEST_SOCKET_NAME),
            "operation": service_wrappers.CONTROL_ATTEST_OPERATION,
            "request_schema": service_wrappers.CONTROL_ATTEST_REQUEST_SCHEMA,
            "response_schema": service_wrappers.CONTROL_ATTEST_RESPONSE_SCHEMA,
            "producer_principal": service_wrappers.CONTROL_ATTEST_PRODUCER,
            "excluded_principal": service_wrappers.CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
            "control_root_owner": service_wrappers.CONTROL_ATTEST_OWNER,
            "control_root_group": service_wrappers.CONTROL_ATTEST_GROUP,
            "control_root_mode": service_wrappers.CONTROL_ROOT_MODE,
            "socket_owner": service_wrappers.CONTROL_ATTEST_OWNER,
            "socket_group": service_wrappers.CONTROL_ATTEST_GROUP,
            "socket_mode": service_wrappers.CONTROL_ATTEST_SOCKET_MODE,
        },
    }


def _wrapper_publication_paths(paths: ActivationPaths) -> tuple[Path, ...]:
    """Return every wrapper API destination in deterministic order."""

    layout = service_wrappers.resolve_layout(**_wrapper_kwargs(paths))
    return tuple(
        path
        for spec in service_wrappers.UNIT_SPECS
        for path in (
            layout.wrapper_path(spec, "start"),
            layout.wrapper_path(spec, "stop"),
        )
    ) + tuple(
        layout.config_path(spec) for spec in service_wrappers.UNIT_SPECS
    ) + (layout.manifest_path,)


def _publish_wrappers(paths: ActivationPaths, release_id: str) -> dict[str, object]:
    """Publish and verify the complete managed wrapper set.

    The wrapper module performs its own atomic publication and readback.  The
    activation receipt records only JSON-safe result metadata; file preimages
    are retained through the transaction's ordinary rollback records.
    """

    try:
        result = service_wrappers.publish_wrappers(
            release_id,
            **_release_wrapper_kwargs(paths, release_id),
        )
    except service_wrappers.WrapperError as exc:
        raise WebActivationError(f"managed wrapper publication failed: {exc}") from exc
    return {
        key: value
        for key, value in result.items()
        if key not in {"snapshot", "rendered"}
    }


def _capture_wrapper_postimages(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    expected_release_id: str,
    *,
    checkpoint: Callable[[str], None],
) -> dict[str, object]:
    """Bind every B wrapper leaf before any later activation boundary.

    The managed-release transaction owns the package state and wrapper set.
    Activation owns an outer inverse for its configuration preimages, so it
    records the exact post-rename identity of every wrapper and manifest leaf
    before runtime, unit, or service work begins.  Preflight on both sides of
    the capture prevents a mixed wrapper generation from becoming a receipt-
    owned B set.
    """

    before = _preflight_watchdog_wrapper(paths, expected_release_id)
    for path in _wrapper_publication_paths(paths):
        key = "wrapper:" + str(path)
        value = preimages.get(key)
        if not isinstance(value, dict):
            raise WebActivationError(f"managed wrapper publication lacks preimage: {path}")
        target = _publication_path(path, label="managed wrapper publication target")
        if _record_target(value) != target:
            raise WebActivationError("managed wrapper publication preimage has the wrong target")
        parent_guards = _record_parent_guards(value, target)
        _path, capture = _capture_anchored(
            target,
            label="managed wrapper publication target",
            parent_guards=parent_guards,
        )
        if (
            not capture.snapshot.exists
            or capture.identity is None
            or capture.identity.nlink != 1
        ):
            raise WebActivationError(f"managed wrapper publication did not create regular target: {target}")
        existing = value.get("postimage")
        if existing is not None:
            _expected_postimage(target, capture, existing)
            continue
        value["postimage"] = _postimage_payload(target, capture, parent_guards)
        checkpoint(key)
    after = _preflight_watchdog_wrapper(paths, expected_release_id)
    if after["manifest_sha256"] != before["manifest_sha256"]:
        raise WebActivationError("managed wrapper manifest changed while binding activation rollback")
    return after


def _release_publication_snapshot_payload(
    path: Path,
    capture: release_manager._AnchoredFileSnapshot,
) -> dict[str, object]:
    """Serialize one release-manager callback snapshot without its bytes.

    The callback itself retains immutable bytes for its duration.  The outer
    activation receipt only needs the exact leaf identity, mode, and digest to
    prove a later public leaf is still the one that callback observed.
    """

    if not isinstance(capture, release_manager._AnchoredFileSnapshot):
        raise WebActivationError("managed release publication callback has an invalid snapshot")
    snapshot = capture.snapshot
    if not snapshot.exists:
        if capture.identity is not None or capture.sha256 is not None:
            raise WebActivationError("managed release publication callback has an invalid absent snapshot")
        return {
            "path": str(path),
            "exists": False,
            "mode": snapshot.mode,
            "sha256": None,
            "identity": None,
        }
    if capture.identity is None or capture.identity.nlink != 1:
        raise WebActivationError("managed release publication callback has an unsafe file identity")
    digest = hashlib.sha256(snapshot.content).hexdigest()
    if capture.sha256 != digest:
        raise WebActivationError("managed release publication callback digest is invalid")
    if capture.identity.mode != snapshot.mode or capture.identity.size != len(snapshot.content):
        raise WebActivationError("managed release publication callback identity is invalid")
    return {
        "path": str(path),
        "exists": True,
        "mode": snapshot.mode,
        "sha256": digest,
        "identity": _file_identity_payload(capture.identity),
    }


def _release_publication_receipt_payload(
    publication: release_manager.PublicationTransactionReceipt,
) -> dict[str, object]:
    """Return the JSON-safe exact callback hand-off retained by activation."""

    if not isinstance(publication, release_manager.PublicationTransactionReceipt):
        raise WebActivationError("managed release publication callback is invalid")
    if publication.state not in {"intent", "published", "recovered"}:
        raise WebActivationError("managed release publication callback state is invalid")
    if publication.journal_identity.nlink != 1:
        raise WebActivationError("managed release publication journal is unsafe")
    parents = []
    for guard in publication.parent_guards.guards:
        parents.append(
            {
                "path": str(guard.path),
                "chain": [
                    _directory_identity_payload(identity)
                    for identity in guard.chain
                ],
            }
        )
    return {
        "journal_path": str(publication.journal_path),
        "journal_identity": _file_identity_payload(publication.journal_identity),
        "state": publication.state,
        "parents": parents,
        "preimages": [
            _release_publication_snapshot_payload(path, capture)
            for path, capture in sorted(publication.preimages.items(), key=lambda item: str(item[0]))
        ],
        "postimages": [
            _release_publication_snapshot_payload(path, capture)
            for path, capture in sorted(publication.postimages.items(), key=lambda item: str(item[0]))
        ],
    }


def _release_publication_record(
    receipt: dict[str, object],
    *,
    key: str,
    operation: str,
    expected_release_id: str | None,
    publication: release_manager.PublicationTransactionReceipt,
) -> dict[str, object]:
    """Create or validate the durable package-publication hand-off record."""

    if operation not in {"install", "rollback"}:
        raise WebActivationError("managed release publication operation is invalid")
    if expected_release_id is not None and (not isinstance(expected_release_id, str) or not expected_release_id):
        raise WebActivationError("managed release publication release id is invalid")
    payload = _release_publication_receipt_payload(publication)
    existing = receipt.get(key)
    if existing is None:
        value: dict[str, object] = {
            "schema": RELEASE_PUBLICATION_SCHEMA,
            "operation": operation,
            "expected_release_id": expected_release_id,
            "state": publication.state,
            "receipt": payload,
        }
        receipt[key] = value
        return value
    if not isinstance(existing, dict):
        raise WebActivationError("managed release publication receipt is invalid")
    expected_fields = {
        "schema", "operation", "expected_release_id", "state", "receipt",
    }
    if (
        set(existing) != expected_fields
        and set(existing) != expected_fields | {"recovery"}
        or existing.get("schema") != RELEASE_PUBLICATION_SCHEMA
        or existing.get("operation") != operation
        or existing.get("expected_release_id") != expected_release_id
    ):
        raise WebActivationError("managed release publication receipt does not match this transaction")
    # A restart recovery of an interrupted B-to-A package inverse restores
    # its authenticated starting B generation.  Retrying that inverse needs a
    # fresh fixed release-manager journal, so its prior journal inode must not
    # be compared to the new callback's intent.  Install callbacks never
    # resume an execution transaction, and consequently cannot reuse this
    # escape hatch.
    if existing.get("state") == "recovered":
        if operation != "rollback" or publication.state != "intent":
            raise WebActivationError("managed release publication recovery cannot resume")
        existing["state"] = "intent"
        existing["receipt"] = payload
        return existing
    stored = existing.get("receipt")
    if not isinstance(stored, Mapping):
        raise WebActivationError("managed release publication callback receipt is invalid")
    # Intent and published receipts have the same journal, parents, and
    # preimages.  Postimages and state intentionally differ at the one
    # publication boundary that this record owns.
    for field in ("journal_path", "parents", "preimages"):
        if stored.get(field) != payload.get(field):
            raise WebActivationError("managed release publication callback changed before completion")
    stored_journal_identity = _file_identity_from_payload(
        stored.get("journal_identity"),
    )
    payload_journal_identity = _file_identity_from_payload(
        payload.get("journal_identity"),
    )
    if (
        stored_journal_identity is None
        or payload_journal_identity is None
        or not _same_publication_journal_inode(
            stored_journal_identity, payload_journal_identity,
        )
    ):
        raise WebActivationError("managed release publication journal changed before completion")
    if existing.get("state") == "published":
        if publication.state != "published" or stored != payload:
            raise WebActivationError("managed release publication postimage changed")
    elif existing.get("state") != "intent" or publication.state not in {"intent", "published"}:
        raise WebActivationError("managed release publication state is invalid")
    return existing


def _adopt_release_wrapper_postimages(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    publication: release_manager.PublicationTransactionReceipt,
    *,
    operation: str,
) -> None:
    """Adopt only wrapper leaves captured by the release-manager journal.

    A package install and an A-to-B inverse are combined release-manager
    transactions.  The outer receipt therefore never re-reads a plausible
    wrapper after their callback: it receives the exact capture made under the
    still-live descriptor journal.  For a managed rollback the recreated A
    inode becomes an authenticated ``rollback_postimage``; for the empty
    first-install inverse, an authenticated absence must agree with the outer
    absent preimage.
    """

    if publication.state != "published":
        raise WebActivationError("managed release publication has no postimages")
    for raw_path in _wrapper_publication_paths(paths):
        path = _publication_path(raw_path, label="managed wrapper publication target")
        key = "wrapper:" + str(path)
        record = preimages.get(key)
        if not isinstance(record, dict):
            raise WebActivationError(f"managed release publication lacks outer preimage: {path}")
        if _record_target(record) != path:
            raise WebActivationError("managed release publication outer preimage has the wrong target")
        outer_guards = _record_parent_guards(record, path)
        callback_guard = publication.parent_guards.for_path(path)
        if callback_guard != outer_guards.for_path(path):
            raise WebActivationError("managed release publication parent guard changed")
        outer_preimage = _receipt_preimage(record, tx)
        callback_preimage = publication.preimages.get(path)
        if not isinstance(callback_preimage, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("managed release publication has no wrapper preimage")
        if operation == "install":
            if callback_preimage != outer_preimage:
                raise WebActivationError("managed release install preimage changed")
        elif operation == "rollback":
            failed_postimage = record.get("postimage")
            if not isinstance(failed_postimage, Mapping):
                raise WebActivationError("managed release rollback lacks a receipt-owned B wrapper")
            _expected_postimage(path, callback_preimage, failed_postimage)
        else:  # defensive narrowing
            raise WebActivationError("managed release publication operation is invalid")
        callback_postimage = publication.postimages.get(path)
        if not isinstance(callback_postimage, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("managed release publication lacks wrapper postimage")
        if operation == "install":
            if (
                not callback_postimage.snapshot.exists
                or callback_postimage.identity is None
                or callback_postimage.identity.nlink != 1
            ):
                raise WebActivationError("managed release install did not publish a complete wrapper set")
            postimage = _postimage_payload(path, callback_postimage, outer_guards)
            existing = record.get("postimage")
            if existing is not None and existing != postimage:
                raise WebActivationError("managed release install wrapper postimage changed")
            record["postimage"] = postimage
            continue

        if operation != "rollback":  # defensive narrowing
            raise WebActivationError("managed release publication operation is invalid")
        if not callback_postimage.snapshot.exists:
            if callback_postimage.identity is not None or outer_preimage.snapshot.exists:
                raise WebActivationError("managed release rollback removed a nonempty wrapper preimage")
            continue
        if callback_postimage.identity is None or callback_postimage.identity.nlink != 1:
            raise WebActivationError("managed release rollback wrapper postimage is unsafe")
        restored = _postimage_payload(path, callback_postimage, outer_guards)
        existing_restored = record.get("rollback_postimage")
        if existing_restored is not None and existing_restored != restored:
            raise WebActivationError("managed release rollback wrapper postimage changed")
        record["rollback_postimage"] = restored


def _release_publication_callbacks(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    preimages: Mapping[str, object],
    *,
    key: str,
    operation: str,
    expected_release_id: str | None,
    checkpoint: Callable[[str], None],
) -> tuple[
    Callable[[release_manager.PublicationTransactionReceipt], None],
    Callable[[release_manager.PublicationTransactionReceipt], None],
]:
    """Build receipt-owning callbacks for one release-manager publication."""

    def before(publication: release_manager.PublicationTransactionReceipt) -> None:
        if publication.state != "intent" or publication.postimages:
            raise WebActivationError("managed release publication intent is invalid")
        _release_publication_record(
            receipt,
            key=key,
            operation=operation,
            expected_release_id=expected_release_id,
            publication=publication,
        )
        if operation == "install" and receipt.get("legacy_bootstrap") is not None:
            from . import legacy_activation
            legacy_activation.capture_package_preimages(paths, tx, receipt, publication)
        checkpoint(f"{operation}:publication-intent")

    def after(publication: release_manager.PublicationTransactionReceipt) -> None:
        if publication.state != "published":
            raise WebActivationError("managed release publication postimage is invalid")
        record = _release_publication_record(
            receipt,
            key=key,
            operation=operation,
            expected_release_id=expected_release_id,
            publication=publication,
        )
        _adopt_release_wrapper_postimages(
            paths,
            tx,
            preimages,
            publication,
            operation=operation,
        )
        if operation == "install" and receipt.get("legacy_bootstrap") is not None:
            from . import legacy_activation
            legacy_activation.capture_package_postimages(paths, tx, receipt, publication)
        record["state"] = "published"
        record["receipt"] = _release_publication_receipt_payload(publication)
        checkpoint(f"{operation}:publication-postimage")

    return before, after


def _assert_release_publication_result(
    result: Mapping[str, object],
    receipt: Mapping[str, object],
    *,
    key: str,
) -> None:
    """Require the public result to repeat the exact callback hand-off."""

    record = receipt.get(key)
    if not isinstance(record, Mapping) or record.get("state") != "published":
        raise WebActivationError("managed release publication did not reach a durable postimage")
    expected = record.get("receipt")
    if not isinstance(expected, Mapping):
        raise WebActivationError("managed release publication receipt is invalid")
    actual = result.get("publication_receipt")
    if actual != expected:
        raise WebActivationError("managed release result does not match its callback receipt")
    wrappers = result.get("service_wrappers")
    if isinstance(wrappers, Mapping) and wrappers.get("publication_receipt") != expected:
        raise WebActivationError("managed release wrapper result does not match its callback receipt")


def _release_publication_record_state(
    receipt: Mapping[str, object],
    *,
    key: str,
) -> tuple[dict[str, object], str] | None:
    """Validate one retained release-manager callback hand-off record."""

    value = receipt.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WebActivationError("managed release publication receipt is invalid")
    expected_fields = {
        "schema", "operation", "expected_release_id", "state", "receipt",
    }
    if (
        set(value) != expected_fields
        and set(value) != expected_fields | {"recovery"}
        or value.get("schema") != RELEASE_PUBLICATION_SCHEMA
    ):
        raise WebActivationError("managed release publication receipt is invalid")
    operation = value.get("operation")
    if operation not in {"install", "rollback"}:
        raise WebActivationError("managed release publication operation is invalid")
    expected_release_id = value.get("expected_release_id")
    if expected_release_id is not None and (
        not isinstance(expected_release_id, str) or not expected_release_id
    ):
        raise WebActivationError("managed release publication release id is invalid")
    state = value.get("state")
    if state not in {"intent", "published", "recovered"}:
        raise WebActivationError("managed release publication state is invalid")
    handoff = value.get("receipt")
    if not isinstance(handoff, Mapping) or set(handoff) != {
        "journal_path", "journal_identity", "state", "parents", "preimages", "postimages",
    }:
        raise WebActivationError("managed release callback hand-off is invalid")
    journal = _stored_publication_path(
        handoff.get("journal_path"),
        label="managed release publication journal",
    )
    identity = _file_identity_from_payload(handoff.get("journal_identity"))
    if identity is None:
        raise WebActivationError("managed release publication journal identity is invalid")
    if handoff.get("state") not in {"intent", "published"}:
        raise WebActivationError("managed release callback hand-off state is invalid")
    if not isinstance(handoff.get("parents"), list) or not isinstance(handoff.get("preimages"), list) or not isinstance(handoff.get("postimages"), list):
        raise WebActivationError("managed release callback hand-off is invalid")
    parents = _release_publication_parent_guards_from_payload(handoff["parents"])
    if parents.for_path(journal) is None:
        raise WebActivationError("managed release publication journal has no parent anchor")
    if state != "recovered" and handoff.get("state") != state:
        raise WebActivationError("managed release publication receipt state is inconsistent")
    return value, str(operation)


def _release_publication_image_map(
    handoff: Mapping[str, object],
    field: str,
) -> dict[Path, dict[str, object]]:
    """Parse one callback image set without accepting duplicate public paths."""

    raw = handoff.get(field)
    if not isinstance(raw, list):
        raise WebActivationError("managed release publication images are invalid")
    result: dict[Path, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "path", "exists", "mode", "sha256", "identity",
        }:
            raise WebActivationError("managed release publication image is invalid")
        path = _stored_publication_path(
            item.get("path"), label="managed release publication target"
        )
        if path in result:
            raise WebActivationError("managed release publication repeats an image")
        exists = item.get("exists")
        identity = _file_identity_from_payload(item.get("identity"))
        mode = item.get("mode")
        digest = item.get("sha256")
        if not isinstance(exists, bool):
            # Keep the check explicit: JSON booleans are the only valid
            # existence marker; integers and strings are not ownership proof.
            raise WebActivationError("managed release publication image is invalid")
        if exists:
            if (
                identity is None
                or identity.nlink != 1
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or mode != identity.mode
                or not _lower_hex(digest, length=64)
            ):
                raise WebActivationError("managed release publication image is invalid")
        elif identity is not None or digest is not None:
            raise WebActivationError("managed release publication absent image is invalid")
        result[path] = dict(item)
    return result


def _release_images_are_exact_subset(
    expected: Mapping[Path, Mapping[str, object]],
    observed: Mapping[Path, Mapping[str, object]],
) -> bool:
    """Require every expected public image in a larger journal image set."""

    return all(observed.get(path) == image for path, image in expected.items())


def _same_release_publication_handoff(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    """Compare a hand-off while allowing its exact journal inode to grow."""

    left_identity = _file_identity_from_payload(left.get("journal_identity"))
    right_identity = _file_identity_from_payload(right.get("journal_identity"))
    return (
        left_identity is not None
        and right_identity is not None
        and _same_publication_journal_inode(left_identity, right_identity)
        and {key: value for key, value in left.items() if key != "journal_identity"}
        == {key: value for key, value in right.items() if key != "journal_identity"}
    )


def _release_publication_recovery_chain(
    record: Mapping[str, object],
) -> list[Mapping[str, object]]:
    recovery = record.get("recovery")
    if recovery is None:
        return []
    # Preserve receipts from the original one-recovery representation.
    if isinstance(recovery, Mapping):
        return [recovery]
    if isinstance(recovery, list) and recovery and all(
        isinstance(item, Mapping) for item in recovery
    ):
        return list(recovery)
    raise WebActivationError("managed release recovery hand-off is invalid")


def _release_publication_recovered_handoff(
    record: Mapping[str, object],
) -> Mapping[str, object]:
    """Return the current B hand-off, including authenticated inverse recovery.

    An interrupted B-to-A transaction can restore B through a fresh
    descriptor exchange.  The fresh inodes are valid only when the release
    manager's recovery callbacks link each exact inverse preimage to the
    preceding B postimages, starting with this record's forward publication.
    This deliberately never relaxes ctime or accepts same-content replacement.
    """

    handoff = record.get("receipt")
    if not isinstance(handoff, Mapping):
        raise WebActivationError("managed release publication receipt is invalid")
    for recovery in _release_publication_recovery_chain(record):
        if (
            set(recovery) != {
                "journal_path", "journal_identity", "state", "parents", "preimages", "postimages",
            }
            or recovery.get("state") != "recovered"
            or _file_identity_from_payload(recovery.get("journal_identity")) is None
            or not isinstance(recovery.get("parents"), list)
        ):
            raise WebActivationError("managed release recovery hand-off is invalid")
        # Every retry is bound to the previous exact B generation.  Keeping
        # the chain proves its ancestry back to the original forward receipt.
        if not _release_images_are_exact_subset(
            _release_publication_image_map(handoff, "postimages"),
            _release_publication_image_map(recovery, "preimages"),
        ):
            raise WebActivationError("managed release recovery is not bound to forward B")
        _release_publication_image_map(recovery, "postimages")
        handoff = recovery
    return handoff


def _outer_postimage_matches_release_image(
    path: Path,
    outer: object,
    image: Mapping[str, object] | None,
) -> bool:
    """Compare a serialized outer postimage to a callback image exactly."""

    if not isinstance(outer, Mapping) or image is None or image.get("exists") is not True:
        return False
    if set(outer) != {"sha256", "size", "mode", "identity", "parent_guard"}:
        return False
    identity = _file_identity_from_payload(image.get("identity"))
    if identity is None:
        return False
    try:
        _parent_guards_from_payload(outer.get("parent_guard"), path)
    except WebActivationError:
        return False
    return (
        outer.get("sha256") == image.get("sha256")
        and outer.get("identity") == image.get("identity")
        and outer.get("mode") == image.get("mode")
        and outer.get("size") == identity.size
    )


def _adopt_recovered_package_b_postimages(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    preimages: Mapping[str, object],
    *,
    inverse_record: dict[str, object],
    publication: release_manager.PublicationTransactionReceipt,
    checkpoint: Callable[[str], None],
) -> None:
    """Persist fresh B identities from an interrupted inverse recovery.

    The callback runs under the release-manager lock while its inverse journal
    still owns the restored leaves.  Link its B preimages to the retained
    forward B receipt, then replace only the outer wrapper postimages with the
    callback's exact recovered B postimages.  This is the sole recovery-only
    identity transition; ordinary ownership checks stay strict.
    """

    if publication.state != "recovered":
        raise WebActivationError("managed release recovery callback state is invalid")
    forward_parsed = _release_publication_record_state(receipt, key="package_publication")
    if forward_parsed is None:
        raise WebActivationError("managed release recovery lacks forward publication")
    forward_record, forward_operation = forward_parsed
    if forward_operation != "install" or forward_record.get("state") != "published":
        raise WebActivationError("managed release recovery lacks published forward B")
    forward_handoff = _release_publication_recovered_handoff(forward_record)
    inverse_handoff = inverse_record.get("receipt")
    if not isinstance(forward_handoff, Mapping) or not isinstance(inverse_handoff, Mapping):
        raise WebActivationError("managed release recovery hand-off is invalid")
    recovered = _release_publication_receipt_payload(publication)
    if recovered.get("state") != "recovered":
        raise WebActivationError("managed release recovery hand-off is invalid")
    # The retained inverse journal is independently authenticated before this
    # callback is registered.  Its preimages must be exactly what recovery
    # reports, and those same images must be the forward B transaction.
    if _release_publication_image_map(recovered, "preimages") != _release_publication_image_map(
        inverse_handoff, "preimages"
    ):
        raise WebActivationError("managed release recovery inverse preimages changed")
    inverse_identity = _file_identity_from_payload(inverse_handoff.get("journal_identity"))
    if (
        recovered.get("journal_path") != inverse_handoff.get("journal_path")
        or recovered.get("parents") != inverse_handoff.get("parents")
        or inverse_identity is None
        or not _same_publication_journal_inode(
            publication.journal_identity,
            inverse_identity,
        )
    ):
        raise WebActivationError("managed release recovery inverse journal changed")
    chain = _release_publication_recovery_chain(forward_record)
    replay = bool(chain) and _same_release_publication_handoff(chain[-1], recovered)
    if not replay and not _release_images_are_exact_subset(
        _release_publication_image_map(forward_handoff, "postimages"),
        _release_publication_image_map(recovered, "preimages"),
    ):
        raise WebActivationError("managed release recovery is not bound to forward B")

    recovered_postimages = _release_publication_image_map(recovered, "postimages")
    recovered_preimages = _release_publication_image_map(recovered, "preimages")
    replacements: list[tuple[dict[str, object], dict[str, object]]] = []
    for raw_path in _wrapper_publication_paths(paths):
        path = _publication_path(raw_path, label="managed wrapper publication target")
        record = preimages.get("wrapper:" + str(path))
        if not isinstance(record, dict) or _record_target(record) != path:
            raise WebActivationError("managed release recovery lacks outer wrapper preimage")
        previous = record.get("postimage")
        if not _outer_postimage_matches_release_image(
            path,
            previous,
            (recovered_postimages if replay else recovered_preimages).get(path),
        ):
            raise WebActivationError("managed release recovery wrapper is not forward B")
        restored = recovered_postimages.get(path)
        if restored is None:
            raise WebActivationError("managed release recovery lacks restored wrapper")
        # The callback snapshot has already verified the parent chain; retain
        # this outer guard so later `_restore` remains descriptor-rooted.
        parent_guards = _record_parent_guards(record, path)
        if publication.parent_guards.for_path(path) != parent_guards.for_path(path):
            raise WebActivationError("managed release recovery wrapper parent changed")
        callback_capture = publication.postimages.get(path)
        if not isinstance(callback_capture, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("managed release recovery lacks callback wrapper postimage")
        replacement = _postimage_payload(path, callback_capture, parent_guards)
        if _release_publication_snapshot_payload(path, callback_capture) != restored:
            raise WebActivationError("managed release recovery wrapper snapshot changed")
        replacements.append((record, replacement))
    for record, replacement in replacements:
        record["postimage"] = replacement
        # The interrupted inverse's A postimage was itself rolled back by the
        # release-manager journal.  It is no longer a live or expected A
        # identity; retaining it would reject the next authenticated inverse
        # merely because that retry creates a new exact A inode.
        record.pop("rollback_postimage", None)

    if replay:
        chain[-1] = recovered
    else:
        chain.append(recovered)
    forward_record["recovery"] = chain[0] if len(chain) == 1 else chain
    # This frame must be durable before the release-manager journal is
    # terminalized.  If it throws, RM retains the exact journal and invokes us
    # again; if it succeeds, a retry has no same-content escape hatch.
    inverse_record["state"] = "recovered"
    checkpoint("rollback:publication-recovered-postimage")


def _recover_pending_release_publications(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    *,
    checkpoint: Callable[[str], None],
) -> None:
    """Resume a durable release journal before deciding whether B is live.

    The callback intent is persisted before release_manager allows any public
    rename.  If an activation process dies there, its fixed combined journal
    is the authority for undoing partial wrapper/state publication.  Recover
    it before selecting a package inverse; otherwise an automatic recovery
    inside ``rollback_release`` can return A and make a stale B-to-A call look
    like an ambiguous no-op.
    """

    for key in ("package_publication", "package_rollback_publication"):
        parsed = _release_publication_record_state(receipt, key=key)
        if parsed is None:
            continue
        record, operation = parsed
        handoff = record.get("receipt")
        if not isinstance(handoff, Mapping):  # narrowed by the parser above
            raise WebActivationError("managed release callback hand-off is invalid")
        journal = _stored_publication_path(
            handoff.get("journal_path"),
            label="managed release publication journal",
        )
        parents = _release_publication_parent_guards_from_payload(handoff["parents"])
        expected_identity = _file_identity_from_payload(handoff.get("journal_identity"))
        if expected_identity is None:
            raise WebActivationError("managed release publication journal identity is invalid")
        _journal_path, current = _capture_anchored(
            journal,
            label="managed release publication journal",
            parent_guards=parents,
        )
        if not current.snapshot.exists:
            continue
        if (
            current.identity is None
            or not _same_publication_journal_inode(
                current.identity, expected_identity,
            )
        ):
            raise WebActivationError("managed release publication journal changed before recovery")
        recovery_callback_fired = False

        def after_recovery(
            publication: release_manager.PublicationTransactionReceipt,
        ) -> None:
            nonlocal recovery_callback_fired
            if publication.state != "recovered":
                raise WebActivationError("managed release recovery callback state is invalid")
            if operation == "rollback":
                _adopt_recovered_package_b_postimages(
                    paths,
                    tx,
                    receipt,
                    receipt.get("preimages") if isinstance(receipt.get("preimages"), Mapping) else {},
                    inverse_record=record,
                    publication=publication,
                    checkpoint=checkpoint,
                )
            else:
                # A recovered forward install returns its original A/empty
                # preimages.  It has no fresh B identity to adopt, but this
                # terminal fact must still be durable before RM retires the
                # exact journal.
                record["state"] = "recovered"
                if receipt.get("legacy_bootstrap") is not None:
                    from . import legacy_activation
                    legacy_activation.capture_package_postimages(
                        paths, tx, receipt, publication, recovered=True,
                    )
                checkpoint(f"{operation}:publication-recovered")
            recovery_callback_fired = True

        try:
            outcome = release_manager.recover_install_publication(
                root=paths.managed_root,
                bin_dir=paths.managed_bin,
                # Forward-install and B-to-A inverse journals now have
                # distinct fixed names.  Recover exactly the receipt-owned
                # leaf whose parent guard and inode were verified above;
                # never let a concurrent retained journal choose an inverse
                # for this outer record.
                journal_path=journal,
                after_recovery=after_recovery,
            )
        except release_manager.ReleaseError as exc:
            raise WebActivationError(
                f"managed release publication recovery failed: {exc}"
            ) from exc
        if not isinstance(outcome, Mapping) or outcome.get("ok") is not True:
            raise WebActivationError("managed release publication recovery did not report success")
        recovered_state = outcome.get("state")
        if not isinstance(recovered_state, str):
            raise WebActivationError("managed release publication recovery has no terminal state")
        if recovered_state.startswith("rolled-back"):
            # The generic journal restored its exact preimages.  For install
            # this returns A (or the empty baseline); for an interrupted
            # B-to-A inverse it returns authenticated B and permits one fresh
            # rollback callback transaction below.
            if not recovery_callback_fired:
                raise WebActivationError("managed release recovery did not return exact postimages")
            continue
        if recovered_state.startswith("committed-cleaned"):
            # A completed B transaction may retain only private cleanup work.
            # It is safe to continue only when the outer callback had already
            # durably bound that exact B generation.
            if record.get("state") != "published":
                raise WebActivationError(
                    "managed release publication committed without an outer postimage"
                )
            continue
        raise WebActivationError("managed release publication recovery has an unexpected state")


def _release_callback_postimage(
    handoff: Mapping[str, object],
    path: Path,
) -> Mapping[str, object]:
    """Return one exact B image from a persisted release callback receipt."""

    raw_postimages = handoff.get("postimages")
    if not isinstance(raw_postimages, list):
        raise WebActivationError("managed release publication postimages are invalid")
    expected = _publication_path(path, label="managed release publication target")
    found: Mapping[str, object] | None = None
    for item in raw_postimages:
        if not isinstance(item, Mapping) or set(item) != {
            "path", "exists", "mode", "sha256", "identity",
        }:
            raise WebActivationError("managed release publication postimage is invalid")
        observed = _stored_publication_path(
            item.get("path"), label="managed release publication target"
        )
        if observed != expected:
            continue
        if found is not None:
            raise WebActivationError("managed release publication repeats a postimage")
        found = item
    if found is None:
        raise WebActivationError(f"managed release publication lacks postimage: {expected}")
    return found


def _assert_package_rollback_owned(
    paths: ActivationPaths,
    receipt: Mapping[str, object],
    preimages: Mapping[str, object],
    *,
    current_release_id: str | None,
    old_release_id: object,
) -> None:
    """Require exact callback ownership before selecting a package inverse.

    The release manager refuses a wrapper/state mismatch itself, but the outer
    transaction must also refuse a current B that was never checkpointed in
    its durable receipt.  In particular this prevents a process death around
    publication from turning a plausible same-content state file into an
    authorized first-install cleanup.
    """

    if current_release_id == old_release_id:
        return
    parsed = _release_publication_record_state(receipt, key="package_publication")
    if parsed is None:
        raise WebActivationError("managed package selection lacks a durable publication receipt")
    record, operation = parsed
    if operation != "install" or record.get("state") != "published":
        raise WebActivationError("managed package selection was not durably published")
    expected_release_id = record.get("expected_release_id")
    if expected_release_id != current_release_id or not isinstance(expected_release_id, str):
        raise WebActivationError("managed package selection does not match its publication receipt")
    handoff = _release_publication_recovered_handoff(record)

    state_path = _publication_path(
        paths.managed_root / "state.json", label="managed release state"
    )
    state_postimage = _release_callback_postimage(handoff, state_path)
    state_snapshot = _managed_release_state_snapshot(paths, require_generation=True)
    if state_snapshot is None:
        raise WebActivationError("managed package state disappeared before rollback")
    if (
        state_postimage.get("exists") is not True
        or state_postimage.get("sha256") != state_snapshot.get("sha256")
        or state_postimage.get("identity") != state_snapshot.get("identity")
    ):
        raise WebActivationError("managed package state changed before rollback")

    # `_adopt_release_wrapper_postimages` bound each wrapper from the same
    # immutable callback receipt before marking the package record published.
    # Re-read them through their original parent FDs here so B remains
    # authenticated until the release manager has selected A/empty legacy.
    for raw_path in _wrapper_publication_paths(paths):
        path = _publication_path(raw_path, label="managed wrapper publication target")
        record_key = "wrapper:" + str(path)
        wrapper_record = preimages.get(record_key)
        if not isinstance(wrapper_record, Mapping) or _record_target(wrapper_record) != path:
            raise WebActivationError("managed package publication lacks a wrapper preimage")
        postimage = wrapper_record.get("postimage")
        if not isinstance(postimage, Mapping):
            raise WebActivationError("managed package publication lacks a wrapper postimage")
        parent_guards = _record_parent_guards(wrapper_record, path)
        _path, current = _capture_anchored(
            path,
            label="managed package wrapper before rollback",
            parent_guards=parent_guards,
        )
        callback_postimage = _release_callback_postimage(handoff, path)
        if path.parent == paths.database.parent and path.name in service_wrappers.UNIT_CONFIG_NAMES:
            tx = paths.evidence / str(receipt.get("tx_id"))
            if tx.parent != paths.evidence or not tx.name.startswith("tx-"):
                raise WebActivationError("config transition transaction is invalid")
            try:
                current, _count = service_wrappers.reconcile_activation_config(
                    tx, path, callback_postimage, parent_guards,
                )
            except service_wrappers.WrapperError as exc:
                raise WebActivationError(str(exc)) from exc
            if not isinstance(wrapper_record, dict):
                raise WebActivationError("config transition lacks a mutable outer receipt")
            postimage = _postimage_payload(path, current, parent_guards)
            wrapper_record["postimage"] = postimage
            callback_postimage = _release_publication_snapshot_payload(path, current)
        _expected_postimage(path, current, postimage)
        if (
            callback_postimage.get("exists") is not True
            or callback_postimage.get("sha256") != postimage.get("sha256")
            or callback_postimage.get("identity") != postimage.get("identity")
        ):
            raise WebActivationError("managed package wrapper is not bound to its publication receipt")


def _copy(
    source: Path,
    destination: Path,
    mode: int,
    *,
    record: dict[str, object] | None = None,
    tx: Path | None = None,
    label: str | None = None,
    checkpoint: Callable[[], None] | None = None,
    intent_checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Copy through a no-follow source read and an anchored destination write.

    A caller that owns a receipt preimage supplies it here so the destination
    can only be changed while that exact leaf and its complete parent chain
    still match the preflight capture.  Staging copies do not have an external
    preimage and use their own local anchored transaction instead.
    """

    _source_path, captured = _read_regular(source, label="activation copy source")
    if not isinstance(captured, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    content = captured.snapshot.content
    operation_label = label or "activation copy destination"
    if record is None:
        return _atomic(destination, content, mode)
    if tx is None:
        raise WebActivationError("transaction preimage copy is missing its transaction")
    return _publish_preimaged(
        record,
        tx,
        content,
        mode,
        label=operation_label,
        checkpoint=checkpoint,
        intent_checkpoint=intent_checkpoint,
    )


def _stage_cargo_binary(binary: Path, cargo_target: Path, destination: Path) -> str:
    """Detach Cargo's verified executable/deps hardlink pair before publication."""

    binary = _publication_path(binary, label="Cargo executable")
    if binary != cargo_target / "release/termdeck-web-server":
        raise WebActivationError("Cargo executable is outside its private build output")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(binary, flags)
    except OSError as exc:
        raise WebActivationError("Cargo executable is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o022
            or not stat.S_IMODE(before.st_mode) & 0o111
            or before.st_nlink not in (1, 2)
        ):
            raise WebActivationError("Cargo executable has an unexpected file identity")
        if before.st_nlink == 2:
            peers = []
            for candidate in (cargo_target / "release/deps").iterdir():
                if not candidate.name.startswith("termdeck_web_server-"):
                    continue
                peer = candidate.lstat()
                if stat.S_ISREG(peer.st_mode) and (peer.st_dev, peer.st_ino) == (before.st_dev, before.st_ino):
                    peers.append(candidate)
            if len(peers) != 1:
                raise WebActivationError("Cargo executable hardlink is not its build artifact")
        if before.st_size > 16 * 1024 * 1024:
            raise WebActivationError("Cargo executable exceeds activation publication capacity")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(before.st_size + 1)
        after = os.fstat(descriptor)
        named = binary.lstat()
        if len(content) != before.st_size or (
            before.st_dev, before.st_ino, before.st_mode, before.st_uid,
            before.st_gid, before.st_nlink, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_gid, after.st_nlink, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise WebActivationError("Cargo executable changed during staging")
        if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise WebActivationError("Cargo executable path changed during staging")
    finally:
        os.close(descriptor)
    _atomic(destination, content, 0o755)
    return hashlib.sha256(content).hexdigest()


def _watchdog_unit(
    paths: ActivationPaths, tx: Path, *, recovery_command: Sequence[str] | None = None,
) -> str:
    # Recovery is itself a managed service.  Its generated wrapper carries
    # the source/evidence/runtime identity and invokes the watchdog using the
    # latest transaction pointer, so this fragment never embeds a stale
    # transaction-specific source command.
    wrapper = paths.managed_root / "termdeck-web-recovery"
    # The generated recovery wrapper already embeds ``--watchdog --latest``.
    # Passing ``--latest`` a second time changes the managed command contract
    # and diverges from the static recovery unit.
    command = shlex.join(recovery_command or [str(wrapper)])
    stop_command = (f"ExecStop={paths.managed_root / 'termdeck-web-recovery-stop'}\n"
                    if recovery_command is None else "")
    runtime_root = _wrapper_runtime_root(paths)
    state_root = paths.database.parent
    writable = " ".join(
        str(path)
        for path in (
            paths.evidence,
            # Canonical runtime contains state. Custom sibling layouts need
            # an explicit grant for config CAS, lifecycle locks and inverse.
            *((state_root,) if not state_root.is_relative_to(paths.runtime) else ()),
            paths.runtime,
            paths.unit_dir,
            paths.managed_root,
            paths.managed_bin,
        )
    )
    required_mounts = " ".join(
        str(path)
        for path in (
            paths.target,
            runtime_root,
            state_root,
        )
    )
    return f"""[Unit]
Description=TermDeck web activation recovery watchdog
After=warp.mount default.target
ConditionPathIsMountPoint=/warp
ConditionPathIsDirectory={paths.target}
ConditionPathIsDirectory={paths.managed_root}
ConditionPathIsDirectory={paths.managed_bin}
# No pending activation is a successful inactive condition; a present LATEST
# is watched until it reaches a terminal state.
ConditionPathExists={paths.latest}
RequiresMountsFor={required_mounts}
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=HOME={state_root}
Environment=USER=max
Environment=LOGNAME=max
Environment=PATH={paths.managed_bin}:/usr/local/bin:/usr/bin:/bin
ExecStart={command}
{stop_command.rstrip()}
Restart=on-failure
RestartMode=direct
KillMode=control-group
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={writable}
TimeoutStartSec=30s
TimeoutStopSec=20s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _sources(paths: ActivationPaths, tx: Path) -> dict[str, tuple[Path, int]]:
    return {
        "bin/termdeck-web-server": (tx / "artifacts/web/termdeck-web-server", 0o755),
        "bin/termdeck-web-prepare": (paths.target / "scripts/termdeck-web-prepare-runtime.sh", 0o755),
        "bin/termdeck-cloudflare-preview": (paths.target / "scripts/termdeck-cloudflare-preview.py", 0o755),
        "nginx/nginx.conf": (paths.target / "deploy/nginx/termdeck-web.nginx.conf", 0o600),
    }


def _web_source_tree(paths: ActivationPaths, commit: str) -> str:
    return _run(
        ["/usr/bin/git", "rev-parse", f"{commit}:web"],
        cwd=paths.target,
        timeout=30,
    ).stdout.strip()


def _publish_units(
    paths: ActivationPaths,
    tx: Path,
    *,
    preimages: Mapping[str, object] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    for unit in SERVICES:
        staged = tx / "staged-units" / unit
        mode = _fingerprint(staged).get("mode", 0o644)
        record: dict[str, object] | None = None
        if preimages is not None:
            value = preimages.get("unit:" + unit)
            if not isinstance(value, dict):
                raise WebActivationError(f"unit publication lacks preimage: {unit}")
            record = value
        _copy(
            staged,
            paths.unit_dir / unit,
            int(mode),
            record=record,
            tx=tx if record is not None else None,
            label=f"activation unit {unit}",
            checkpoint=(
                (lambda unit=unit: checkpoint("unit:" + unit))
                if record is not None and checkpoint is not None
                else None
            ),
            intent_checkpoint=(
                (lambda unit=unit: checkpoint("intent:unit:" + unit))
                if record is not None and checkpoint is not None
                else None
            ),
        )


def prepare(
    paths: ActivationPaths,
    *,
    reuse_web_binary: Path | None = None,
    reuse_web_source_commit: str | None = None,
    migrate_from_root: Path | None = None,
    migrate_from_bin_dir: Path | None = None,
) -> dict[str, object]:
    if (migrate_from_root is None) != (migrate_from_bin_dir is None):
        raise WebActivationError("legacy path migration requires both explicit historical paths")
    source = _source(paths)
    tx, receipt = new_transaction(paths, source)
    if migrate_from_root is not None:
        receipt["legacy_path_migration_request"] = {
            "source_root": str(migrate_from_root), "source_bin_dir": str(migrate_from_bin_dir),
        }
    try:
        artifacts = tx / "artifacts"
        package_dir = artifacts / "package"
        package_dir.mkdir(parents=True, mode=0o700)
        _persist(paths, tx, receipt, "building-package")
        package = build_release(paths.target, package_dir, allow_dirty=False, python=sys.executable)
        receipt["package"] = {key: package[key] for key in ("release_id", "wheel", "manifest", "wheel_sha256")}
        receipt["package"].update({
            "version": (package.get("build_info") or {}).get("version"),
            "source_commit": (package.get("source") or {}).get("commit"),
            "source_tree_sha256": (package.get("source") or {}).get("tree_sha256"),
        })
        _persist(paths, tx, receipt, "building-web")
        cargo_target = artifacts / "cargo-target"
        staged_binary = artifacts / "web/termdeck-web-server"
        current_web_tree = _web_source_tree(paths, source["commit"])
        if reuse_web_binary is not None:
            reuse_web_binary = reuse_web_binary.resolve()
            if not reuse_web_binary.is_file() or not os.access(reuse_web_binary, os.X_OK):
                raise WebActivationError("qualified reused web binary is unavailable")
            reused_commit = reuse_web_source_commit or source["commit"]
            reused_web_tree = _web_source_tree(paths, reused_commit)
            if reused_web_tree != current_web_tree:
                raise WebActivationError("reused web binary source tree differs from checkout")
            _run([str(reuse_web_binary), "--help"], cwd=paths.target / "web", timeout=30)
            binary = reuse_web_binary
            receipt["web_binary"] = {
                "reused": True,
                "source_commit": reused_commit,
                "source_tree": reused_web_tree,
                "current_source_tree": current_web_tree,
                "sha256": _sha(reuse_web_binary),
            }
            _copy(binary, staged_binary, 0o755)
        else:
            environment = dict(os.environ)
            environment["CARGO_TARGET_DIR"] = str(cargo_target)
            _run(["/usr/bin/cargo", "build", "--release", "--locked", "-p", "termdeck-web-server"],
                 cwd=paths.target / "web", env=environment, timeout=1800)
            binary = cargo_target / "release/termdeck-web-server"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise WebActivationError("staged web build did not produce an executable")
            _run([str(binary), "--help"], cwd=paths.target / "web", timeout=30)
            binary_sha256 = _stage_cargo_binary(binary, cargo_target, staged_binary)
            receipt["web_binary"] = {
                "reused": False,
                "source_commit": source["commit"],
                "source_tree": current_web_tree,
                "current_source_tree": current_web_tree,
                "sha256": binary_sha256,
            }
        staged_runtime, staged_units = tx / "staged-runtime", tx / "staged-units"
        for relative, (source_path, mode) in _sources(paths, tx).items():
            _source_path, source_capture = _read_regular(
                source_path, label=f"activation runtime source {relative}",
            )
            if (
                not isinstance(source_capture, release_manager._AnchoredFileSnapshot)
                or OBSOLETE_RUNTIME_MARKER in source_capture.snapshot.content
            ):
                raise WebActivationError(f"obsolete runtime marker in {relative}")
            _copy(source_path, staged_runtime / relative, mode)
        for unit in SERVICES:
            source_path = paths.target / "deploy/systemd" / unit
            _source_path, source_capture = _read_regular(
                source_path, label=f"activation unit source {unit}",
            )
            if (
                not isinstance(source_capture, release_manager._AnchoredFileSnapshot)
                or OBSOLETE_RUNTIME_MARKER in source_capture.snapshot.content
            ):
                raise WebActivationError(f"obsolete runtime marker in {unit}")
            _copy(source_path, staged_units / unit, 0o644)
        _atomic(staged_units / WATCHDOG_UNIT, _watchdog_unit(paths, tx).encode(), 0o644)
        files = {}
        for root in (artifacts / "package", artifacts / "web", staged_runtime, staged_units):
            files.update({str(p.relative_to(tx)): _fingerprint(p) for p in root.rglob("*") if p.is_file()})
        receipt["artifacts"] = {"files": files, "staged_runtime": {key: _fingerprint(staged_runtime / key) for key in _sources(paths, tx)}, "staged_units": {key: _fingerprint(staged_units / key) for key in (*SERVICES, WATCHDOG_UNIT)}}
        _persist(paths, tx, receipt, "artifacts-staged")
        return {"ok": True, "action": "prepared", "transaction": str(tx), "receipt": receipt}
    except Exception as exc:
        receipt["error"] = str(exc)
        _persist(paths, tx, receipt, "failed")
        raise


def _record_target(record: Mapping[str, object]) -> Path:
    return _stored_publication_path(record.get("path"), label="activation preimage target")


def _record_parent_guards(record: Mapping[str, object], path: Path) -> object:
    return _parent_guards_from_payload(record.get("parent_guard"), path)


def _backup_path(record: Mapping[str, object], tx: Path) -> Path:
    raw = record.get("backup")
    if not isinstance(raw, str) or not raw:
        raise WebActivationError("activation preimage backup is missing")
    relative = Path(raw)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise WebActivationError("activation preimage backup path is invalid")
    root = _publication_path(tx, label="activation transaction")
    candidate = _publication_path(root / relative, label="activation preimage backup")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WebActivationError("activation preimage backup escapes transaction") from exc
    return candidate


def _receipt_preimage(
    record: Mapping[str, object], tx: Path,
) -> release_manager._AnchoredFileSnapshot:
    exists = record.get("exists")
    if not isinstance(exists, bool):
        raise WebActivationError("activation preimage existence is invalid")
    identity = _file_identity_from_payload(record.get("preimage_identity"))
    if not exists:
        if record.get("backup") is not None or identity is not None:
            raise WebActivationError("absent activation preimage has invalid backup state")
        return release_manager._AnchoredFileSnapshot(
            release_manager._FileSnapshot(False), None, None,
        )

    backup = _backup_path(record, tx)
    backup_postimage = record.get("backup_postimage")
    if not isinstance(backup_postimage, Mapping):
        raise WebActivationError("activation preimage backup has no postimage")
    backup_guards = _parent_guards_from_payload(backup_postimage.get("parent_guard"), backup)
    _path, backup_capture = _capture_anchored(
        backup,
        label="activation preimage backup",
        parent_guards=backup_guards,
    )
    _expected_postimage(backup, backup_capture, backup_postimage)
    if not isinstance(backup_capture, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    digest = record.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise WebActivationError("activation preimage digest is invalid")
    mode = _integer(record.get("mode"), label="activation preimage mode")
    size = _integer(record.get("size"), label="activation preimage size")
    if (
        not backup_capture.snapshot.exists
        or hashlib.sha256(backup_capture.snapshot.content).hexdigest() != digest
        or len(backup_capture.snapshot.content) != size
        or backup_capture.snapshot.mode != mode
        or identity is None
    ):
        raise WebActivationError("activation preimage backup does not match its receipt")
    return release_manager._AnchoredFileSnapshot(
        release_manager._FileSnapshot(True, backup_capture.snapshot.content, mode),
        identity,
        digest,
    )


def _publication_journal_path(path: Path, tx: Path) -> Path:
    """Return the deterministic inner-journal name owned by one outer leaf.

    A generic release-manager publication normally chooses an ephemeral journal
    name.  The activation receipt needs a stable name before the first public
    rename so a new process can ask the release manager to authenticate and
    recover an interrupted B rather than guessing from matching bytes.
    """

    target = _publication_path(path, label="activation publication target")
    transaction = _publication_path(tx, label="activation transaction")
    digest = hashlib.sha256(
        (str(transaction) + "\0" + str(target)).encode("utf-8")
    ).hexdigest()
    return _publication_path(
        target.parent / f".termdeck-publication-web-activation-{digest}.json",
        label="activation publication journal",
    )


def _publication_intent(
    record: dict[str, object],
    path: Path,
    tx: Path,
    content: bytes,
    mode: int,
) -> tuple[dict[str, object], Path]:
    """Create or validate the durable outer intent for one B publication."""

    if not isinstance(content, bytes):
        raise WebActivationError("activation publication content is invalid")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise WebActivationError("activation publication mode is invalid")
    expected_path = _publication_path(path, label="activation publication target")
    expected_journal = _publication_journal_path(expected_path, tx)
    expected_digest = hashlib.sha256(content).hexdigest()
    existing = record.get("publication_intent")
    if existing is None:
        value: dict[str, object] = {
            "schema": PUBLICATION_INTENT_SCHEMA,
            "path": str(expected_path),
            "journal": str(expected_journal),
            # The outer receipt first records the deterministic journal name.
            # Once release_manager has durably created that leaf, its
            # ``before_publish`` callback fills this exact identity before a
            # public B rename is allowed to begin.
            "journal_identity": None,
            "sha256": expected_digest,
            "size": len(content),
            "mode": mode,
            "state": "prepared",
        }
        record["publication_intent"] = value
        return value, expected_journal
    if not isinstance(existing, dict):
        raise WebActivationError("activation publication intent is invalid")
    expected_fields = {
        "schema", "path", "journal", "journal_identity", "sha256",
        "size", "mode", "state",
    }
    if set(existing) != expected_fields or existing.get("schema") != PUBLICATION_INTENT_SCHEMA:
        raise WebActivationError("activation publication intent is invalid")
    stored_path = _stored_publication_path(existing.get("path"), label="activation publication intent target")
    stored_journal = _stored_publication_path(
        existing.get("journal"), label="activation publication intent journal",
    )
    if stored_path != expected_path or stored_journal != expected_journal:
        raise WebActivationError("activation publication intent does not match its target")
    digest = existing.get("sha256")
    if not isinstance(digest, str) or digest != expected_digest:
        raise WebActivationError("activation publication intent digest does not match its target")
    if _integer(existing.get("size"), label="activation publication intent size") != len(content):
        raise WebActivationError("activation publication intent size does not match its target")
    if _integer(existing.get("mode"), label="activation publication intent mode") != mode:
        raise WebActivationError("activation publication intent mode does not match its target")
    state = existing.get("state")
    if state not in {"prepared", "journal-intent", "postimage-recorded", "recovered"}:
        raise WebActivationError("activation publication intent state is invalid")
    journal_identity = _file_identity_from_payload(existing.get("journal_identity"))
    if (state == "prepared") != (journal_identity is None):
        raise WebActivationError("activation publication intent journal identity is invalid")
    return existing, expected_journal


def _same_preimage_inode(
    current: release_manager._AnchoredFileSnapshot,
    preimage: release_manager._AnchoredFileSnapshot,
) -> bool:
    """Recognize A only when its original inode still owns the public leaf.

    Renaming an authenticated A tombstone back into place updates ctime, so
    equality intentionally permits that one metadata effect.  It still binds
    device, inode, hard-link count, mode, size, mtime, and content; a fresh
    same-content foreign C therefore cannot masquerade as a completed restore.
    """

    if current.snapshot != preimage.snapshot:
        return False
    if not preimage.snapshot.exists:
        return current.identity is None and preimage.identity is None
    if current.identity is None or preimage.identity is None:
        return False
    return (
        current.identity.device == preimage.identity.device
        and current.identity.inode == preimage.identity.inode
        and current.identity.mode == preimage.identity.mode
        and current.identity.nlink == preimage.identity.nlink == 1
        and current.identity.size == preimage.identity.size
        and current.identity.mtime_ns == preimage.identity.mtime_ns
    )


def _same_publication_journal_inode(
    left: release_manager._FileIdentity,
    right: release_manager._FileIdentity,
) -> bool:
    """Compare the immutable identity of an append-only journal leaf.

    A publication journal deliberately grows and fsyncs new frames between its
    intent and postimage callbacks.  Its size and timestamps therefore cannot
    stay equal, while device/inode/mode/nlink still bind the one writable
    journal inode that release_manager validates frame-by-frame.
    """

    return (
        left.device == right.device
        and left.inode == right.inode
        and left.mode == right.mode
        and left.nlink == right.nlink == 1
    )


def _rollback_publication_journal_path(path: Path, tx: Path) -> Path:
    """Keep an inverse journal distinct from the retired forward journal."""

    forward = _publication_journal_path(path, tx)
    return forward.with_name(forward.stem + "-rollback.json")


def _checkpoint_rollback_recovery(
    record: dict[str, object],
    publication: release_manager.PublicationTransactionReceipt,
    *,
    journal: Path,
    journal_identity: release_manager._FileIdentity,
    parent_guards: object,
    before_images: Mapping[Path, Mapping[str, object]],
    checkpoint: Callable[[], None] | None,
) -> None:
    """Bind the exact recovered B before its inverse journal can be retired."""

    path = _record_target(record)
    value = record.get("rollback_publication")
    if not isinstance(value, dict):
        raise WebActivationError("activation rollback recovery lost its intent")
    recovered = _release_publication_receipt_payload(publication)
    if (
        publication.state != "recovered"
        or publication.journal_path != journal
        or not _same_publication_journal_inode(publication.journal_identity, journal_identity)
        or publication.parent_guards != parent_guards
        or _release_publication_image_map(recovered, "preimages") != before_images
        or set(before_images) != {path}
        or set(publication.postimages) != {path}
    ):
        raise WebActivationError("activation rollback recovery callback changed")
    previous_recovery = value.get("recovery")
    if previous_recovery is not None and (
        not isinstance(previous_recovery, Mapping)
        or not _same_release_publication_handoff(previous_recovery, recovered)
    ):
        raise WebActivationError("activation rollback recovered postimage changed")
    restored = publication.postimages[path]
    if (
        not restored.snapshot.exists or restored.identity is None
        or restored.snapshot.mode != before_images[path].get("mode")
        or restored.sha256 != before_images[path].get("sha256")
    ):
        raise WebActivationError("activation rollback recovery lost its B postimage")
    record["postimage"] = _postimage_payload(path, restored, parent_guards)
    record.pop("rollback_postimage", None)
    value["state"] = "recovered"
    value["recovery"] = recovered
    if checkpoint is not None:
        checkpoint()


def _recover_record_rollback_publication(
    record: dict[str, object],
    tx: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Recover only a receipt-bound per-file inverse and adopt its exact B."""

    value = record.get("rollback_publication")
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) not in (
            {"schema", "state", "receipt"},
            {"schema", "state", "receipt", "recovery"},
        )
        or value.get("schema") != "termdeck.web-activation-file-rollback.v1"
        or value.get("state") not in {"intent", "published", "recovered"}
    ):
        raise WebActivationError("activation rollback publication receipt is invalid")
    handoff = value.get("receipt")
    if not isinstance(handoff, Mapping) or set(handoff) != {
        "journal_path", "journal_identity", "state", "parents", "preimages", "postimages",
    }:
        raise WebActivationError("activation rollback publication hand-off is invalid")
    if handoff.get("state") not in {"intent", "published"} or (
        value["state"] != "recovered" and handoff["state"] != value["state"]
    ):
        raise WebActivationError("activation rollback publication state is invalid")
    path = _record_target(record)
    parent_guards = _record_parent_guards(record, path)
    journal = _stored_publication_path(
        handoff.get("journal_path"), label="activation rollback publication journal",
    )
    journal_identity = _file_identity_from_payload(handoff.get("journal_identity"))
    if (
        journal != _rollback_publication_journal_path(path, tx)
        or journal_identity is None
        or journal_identity.nlink != 1
        or _release_publication_parent_guards_from_payload(handoff.get("parents")) != parent_guards
    ):
        raise WebActivationError("activation rollback publication ownership is invalid")
    before_images = _release_publication_image_map(handoff, "preimages")
    after_images = _release_publication_image_map(handoff, "postimages")
    if set(before_images) != {path} or (
        set(after_images) != ({path} if handoff["state"] == "published" else set())
    ):
        raise WebActivationError("activation rollback publication destinations changed")
    recovery = value.get("recovery")
    if (value["state"] == "recovered") != (recovery is not None):
        raise WebActivationError("activation rollback recovery state is invalid")
    if recovery is not None:
        if (
            not isinstance(recovery, Mapping)
            or recovery.get("state") != "recovered"
            or recovery.get("journal_path") != str(journal)
            or recovery.get("parents") != handoff.get("parents")
            or _release_publication_image_map(recovery, "preimages") != before_images
            or set(_release_publication_image_map(recovery, "postimages")) != {path}
        ):
            raise WebActivationError("activation rollback recovery hand-off is invalid")
        recovery_identity = _file_identity_from_payload(recovery.get("journal_identity"))
        if recovery_identity is None or not _same_publication_journal_inode(
            recovery_identity, journal_identity,
        ):
            raise WebActivationError("activation rollback recovery journal changed")
    owned_b = (
        _release_publication_image_map(recovery, "postimages")[path]
        if isinstance(recovery, Mapping) else before_images[path]
    )
    if not _outer_postimage_matches_release_image(path, record.get("postimage"), owned_b):
        raise WebActivationError("activation rollback publication is not bound to its B postimage")

    callback_fired = False

    def after_recovery(publication: release_manager.PublicationTransactionReceipt) -> None:
        nonlocal callback_fired
        _checkpoint_rollback_recovery(
            record, publication, journal=journal, journal_identity=journal_identity,
            parent_guards=parent_guards, before_images=before_images, checkpoint=checkpoint,
        )
        callback_fired = True

    _journal_path, current_journal = _capture_anchored(
        journal, label="activation rollback publication journal", parent_guards=parent_guards,
    )
    if current_journal.snapshot.exists:
        if current_journal.identity is None or not _same_publication_journal_inode(
            current_journal.identity, journal_identity,
        ):
            raise WebActivationError("activation rollback publication journal changed before recovery")
        try:
            outcome = release_manager.recover_publication_transaction(
                journal, after_recovery=after_recovery,
            )
        except release_manager.ReleaseError as exc:
            raise WebActivationError(f"activation rollback publication recovery failed: {exc}") from exc
        if not isinstance(outcome, Mapping) or outcome.get("ok") is not True:
            raise WebActivationError("activation rollback publication recovery did not report success")
        state = outcome.get("state")
        if isinstance(state, str) and state.startswith("rolled-back"):
            if not callback_fired:
                raise WebActivationError("activation rollback recovery lacks exact postimages")
        elif not (
            isinstance(state, str)
            and state.startswith("committed-cleaned")
            and value["state"] == "published"
        ):
            raise WebActivationError("activation rollback publication recovery state is invalid")

    _path, current = _capture_anchored(
        path, label="activation rollback recovery target", parent_guards=parent_guards,
    )
    if value["state"] == "published":
        preimage = _receipt_preimage(record, tx)
        image = after_images[path]
        if (
            image.get("exists") != preimage.snapshot.exists
            or image.get("mode") != preimage.snapshot.mode
            or image.get("sha256") != preimage.sha256
        ):
            raise WebActivationError("activation rollback publication changed its A preimage")
        if preimage.snapshot.exists:
            if not _outer_postimage_matches_release_image(path, record.get("rollback_postimage"), image):
                raise WebActivationError("activation rollback publication lacks its exact A postimage")
            _expected_postimage(path, current, record["rollback_postimage"])
        elif current.snapshot.exists:
            raise WebActivationError(f"activation target changed before rollback: {path}")
    else:
        _expected_postimage(path, current, record["postimage"])


def _recover_record_publication(
    record: dict[str, object],
    tx: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> release_manager._AnchoredFileSnapshot | None:
    """Resolve a bound journal, or return the exact untouched A for revalidation."""

    _recover_record_rollback_publication(record, tx, checkpoint=checkpoint)
    path = _record_target(record)
    parent_guards = _record_parent_guards(record, path)
    intent = record.get("publication_intent")
    if intent is None:
        return
    # Reuse the strict intent parser without guessing B from its public bytes.
    postimage = record.get("postimage")
    if isinstance(postimage, Mapping):
        digest = postimage.get("sha256")
        size = postimage.get("size")
        mode = postimage.get("mode")
        if not isinstance(digest, str) or len(digest) != 64:
            raise WebActivationError("activation postimage cannot bind its publication intent")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WebActivationError("activation postimage cannot bind its publication intent")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            raise WebActivationError("activation postimage cannot bind its publication intent")
        # The intent itself is validated below; passing reconstructed content
        # is impossible from only a digest, so inspect its fixed fields here.
        if not isinstance(intent, Mapping):
            raise WebActivationError("activation publication intent is invalid")
        expected_fields = {
            "schema", "path", "journal", "journal_identity", "sha256",
            "size", "mode", "state",
        }
        if set(intent) != expected_fields or intent.get("schema") != PUBLICATION_INTENT_SCHEMA:
            raise WebActivationError("activation publication intent is invalid")
        journal = _stored_publication_path(intent.get("journal"), label="activation publication intent journal")
        if _stored_publication_path(intent.get("path"), label="activation publication intent target") != path:
            raise WebActivationError("activation publication intent does not match its target")
        if journal != _publication_journal_path(path, tx):
            raise WebActivationError("activation publication intent journal does not match its target")
        if intent.get("sha256") != digest or intent.get("size") != size or intent.get("mode") != mode:
            raise WebActivationError("activation publication intent does not bind its postimage")
        if intent.get("state") not in {"prepared", "journal-intent", "postimage-recorded", "recovered"}:
            raise WebActivationError("activation publication intent state is invalid")
    else:
        # An intent without a receipt-owned B has no byte-based fallback.  Its
        # named release-manager journal is the sole recovery authority.
        if not isinstance(intent, Mapping):
            raise WebActivationError("activation publication intent is invalid")
        expected_fields = {
            "schema", "path", "journal", "journal_identity", "sha256",
            "size", "mode", "state",
        }
        if set(intent) != expected_fields or intent.get("schema") != PUBLICATION_INTENT_SCHEMA:
            raise WebActivationError("activation publication intent is invalid")
        if _stored_publication_path(intent.get("path"), label="activation publication intent target") != path:
            raise WebActivationError("activation publication intent does not match its target")
        journal = _stored_publication_path(intent.get("journal"), label="activation publication intent journal")
        if journal != _publication_journal_path(path, tx):
            raise WebActivationError("activation publication intent journal does not match its target")
        if (
            not isinstance(intent.get("sha256"), str)
            or len(str(intent.get("sha256"))) != 64
            or isinstance(intent.get("size"), bool)
            or not isinstance(intent.get("size"), int)
            or int(intent["size"]) < 0
            or isinstance(intent.get("mode"), bool)
            or not isinstance(intent.get("mode"), int)
            or not 0 <= int(intent["mode"]) <= 0o7777
            or intent.get("state") not in {"prepared", "journal-intent", "postimage-recorded", "recovered"}
        ):
            raise WebActivationError("activation publication intent is invalid")

    journal_identity = _file_identity_from_payload(intent.get("journal_identity"))
    if (intent.get("state") == "prepared") != (journal_identity is None):
        raise WebActivationError("activation publication intent journal identity is invalid")

    # The journal is in the target's anchored parent.  A dangling/symlinked
    # candidate fails the anchored capture before any recovery decision.
    _journal_path, journal_capture = _capture_anchored(
        journal,
        label="activation publication journal",
        parent_guards=parent_guards,
    )
    if journal_capture.snapshot.exists:
        if journal_identity is None:
            # ``before_publish`` has not durably returned, so no public B
            # rename was authorized.  Do not adopt or replay an unbound
            # journal name.  Only an untouched preimage with no contradictory
            # publication ownership is safe to leave alone.  Unlike a bound
            # tombstone inverse, no authorized rename can explain ctime drift.
            if (
                postimage is None
                and record.get("rollback_postimage") is None
                and record.get("rollback_publication") is None
            ):
                _path, current = _capture_anchored(
                    path, label="activation unpublished target", parent_guards=parent_guards,
                )
                preimage = _receipt_preimage(record, tx)
                if current == preimage:
                    return preimage
            raise WebActivationError("activation publication journal was not receipt-bound before publication")
        if (
            journal_capture.identity is None
            or not _same_publication_journal_inode(
                journal_capture.identity, journal_identity,
            )
        ):
            raise WebActivationError("activation publication journal changed before recovery")
        try:
            release_manager.recover_publication_transaction(journal)
        except release_manager.ReleaseError as exc:
            raise WebActivationError(f"activation publication recovery failed: {exc}") from exc
        if not isinstance(intent, dict):  # narrowed above
            raise WebActivationError("activation publication intent is invalid")
        intent["state"] = "recovered"
        if checkpoint is not None:
            checkpoint()
        return

    # A completed inner transaction deletes its journal only after the outer
    # receipt checkpoint has captured B.  Without that checkpoint, only the
    # exact original A inode is safe to accept; matching content alone is a
    # foreign-leaf collision and deliberately remains actionable.
    _path, current = _capture_anchored(
        path,
        label="activation recovery target",
        parent_guards=parent_guards,
    )
    preimage = _receipt_preimage(record, tx)
    if _same_preimage_inode(current, preimage):
        return
    # A crash can happen after the inverse exchange made A public, but before
    # the caller wrote its ordinary ``rollback_restore`` progress marker.  The
    # inverse publication callback records the exact newly-public A identity
    # while its own journal is still retained.  Recognize only that receipt-
    # owned inode here; a freshly-created same-byte C must still fail below.
    restored_postimage = record.get("rollback_postimage")
    if restored_postimage is not None:
        if not isinstance(restored_postimage, Mapping):
            raise WebActivationError("activation rollback postimage is invalid")
        try:
            _expected_postimage(path, current, restored_postimage)
        except WebActivationError as exc:
            # A current B can legitimately differ after an interrupted inner
            # inverse recovered back to B.  Malformed receipt data and parent
            # guard failures are security boundaries and remain fatal.
            if not (
                str(exc).startswith("activation target changed before rollback")
                or str(exc).startswith("activation target disappeared before rollback")
            ):
                raise
        else:
            return
    if isinstance(postimage, Mapping):
        _expected_postimage(path, current, postimage)
        return
    raise WebActivationError("activation publication lost its recovery journal before B was receipt-owned")


def _preimage(path: Path, tx: Path, name: str) -> dict[str, object]:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise WebActivationError("activation preimage name is invalid")
    candidate = _publication_path(path, label="activation preimage target")
    parent_guards = _capture_parent_guards((candidate,), create_parents=True)
    _path, capture = _capture_anchored(
        candidate,
        label="activation preimage target",
        parent_guards=parent_guards,
    )
    if not isinstance(capture, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    record = _fingerprint_from_capture(candidate, capture)
    record["parent_guard"] = _parent_guard_payload(parent_guards, candidate)
    record["preimage_identity"] = _file_identity_payload(capture.identity)
    record["postimage"] = None
    if capture.snapshot.exists:
        root = _publication_path(tx, label="activation transaction")
        backup = _publication_path(root / "preimages" / name, label="activation preimage backup")
        record["backup_postimage"] = _atomic(backup, capture.snapshot.content, capture.snapshot.mode)
        record["backup"] = str(backup.relative_to(root))
    else:
        record["backup"] = None
        record["backup_postimage"] = None
    return record


def _publish_preimaged(
    record: dict[str, object],
    tx: Path,
    content: bytes,
    mode: int,
    *,
    label: str,
    checkpoint: Callable[[], None] | None = None,
    intent_checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    path = _record_target(record)
    parent_guards = _record_parent_guards(record, path)
    preimage = _receipt_preimage(record, tx)
    intent, journal = _publication_intent(record, path, tx, content, mode)
    # The receipt-owned intent precedes the release-manager journal creation.
    # If the process dies before the inner callback, no public rename has
    # been authorized.  The callback then persists the journal's exact inode
    # before any staged/public B step; a later crash can recover only that
    # authenticated transaction rather than infer B from matching bytes.
    if intent_checkpoint is not None:
        intent_checkpoint()

    def checkpoint_journal(
        publication: release_manager.PublicationTransactionReceipt,
    ) -> None:
        if (
            publication.state != "intent"
            or publication.journal_path != journal
            or publication.parent_guards != parent_guards
            or publication.preimages.get(path) != preimage
            or publication.postimages
        ):
            raise WebActivationError("activation publication journal hand-off is invalid")
        identity = publication.journal_identity
        if identity.nlink != 1:
            raise WebActivationError("activation publication journal is unsafe")
        intent["journal_identity"] = _file_identity_payload(identity)
        intent["state"] = "journal-intent"
        if intent_checkpoint is not None:
            intent_checkpoint()

    def checkpoint_postimage(postimage: dict[str, object]) -> None:
        record["postimage"] = postimage
        intent["state"] = "postimage-recorded"
        if checkpoint is not None:
            checkpoint()

    postimage = _publish_anchored(
        path,
        content,
        mode,
        label=label,
        parent_guards=parent_guards,
        expected_preimage=preimage,
        journal_path=journal,
        intent_checkpoint=checkpoint_journal,
        postimage_checkpoint=checkpoint_postimage,
    )
    if postimage is None:
        raise WebActivationError(f"activation publication unexpectedly removed: {path}")
    return postimage


def _restore(
    record: dict[str, object],
    tx: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    path = _record_target(record)
    parent_guards = _record_parent_guards(record, path)
    unpublished_preimage = _recover_record_publication(record, tx, checkpoint=checkpoint)
    _path, current = _capture_anchored(
        path,
        label="activation rollback target",
        parent_guards=parent_guards,
    )
    preimage = _receipt_preimage(record, tx)
    if not isinstance(current, release_manager._AnchoredFileSnapshot):  # defensive narrowing
        raise WebActivationError("invalid anchored activation file capture")
    if unpublished_preimage is not None:
        # Preserve the stricter no-publication proof across the outer read;
        # generic authenticated tombstone recovery below may tolerate ctime.
        if current != unpublished_preimage or preimage != unpublished_preimage:
            raise WebActivationError(f"activation unpublished target changed before rollback: {path}")
        return "already-restored"
    # A generic publication journal can restore its original A tombstone with
    # the same inode (ctime changes on the rename).  A direct outer rollback
    # records the new A identity while that journal is still active.  Neither
    # path accepts matching bytes from a fresh foreign leaf.
    if _same_preimage_inode(current, preimage):
        return "already-restored"
    restored_postimage = record.get("rollback_postimage")
    if restored_postimage is not None:
        if not isinstance(restored_postimage, Mapping):
            raise WebActivationError("activation rollback postimage is invalid")
        try:
            _expected_postimage(path, current, restored_postimage)
        except WebActivationError as exc:
            # A current B can legitimately differ from the completed A
            # postimage after the inner recovery journal rolled the interrupted
            # restore back to B.  Parse/parent errors still fail closed; only
            # an ordinary leaf mismatch may proceed to authenticate B below.
            if not str(exc).startswith("activation target changed before rollback") and not str(exc).startswith("activation target disappeared before rollback"):
                raise
        else:
            return "already-restored"
    postimage = record.get("postimage")
    if not isinstance(postimage, Mapping):
        raise WebActivationError(
            f"activation rollback refuses unowned target without a transaction-owned postimage: {path}"
        )
    postimage_guards = _parent_guards_from_payload(postimage.get("parent_guard"), path)
    if postimage_guards != parent_guards:
        raise WebActivationError("activation postimage parent guard does not match its preimage")
    expected_postimage = _expected_postimage(path, current, postimage)

    journal = _rollback_publication_journal_path(path, tx)

    def checkpoint_intent(publication: release_manager.PublicationTransactionReceipt) -> None:
        if (
            publication.state != "intent"
            or publication.journal_path != journal
            or publication.parent_guards != parent_guards
            or dict(publication.preimages) != {path: expected_postimage}
            or publication.postimages
        ):
            raise WebActivationError("activation rollback publication intent changed")
        record["rollback_publication"] = {
            "schema": "termdeck.web-activation-file-rollback.v1",
            "state": "intent",
            "receipt": _release_publication_receipt_payload(publication),
        }
        if checkpoint is not None:
            checkpoint()

    def checkpoint_restored(publication: release_manager.PublicationTransactionReceipt) -> None:
        value = record.get("rollback_publication")
        if not isinstance(value, dict) or not isinstance(value.get("receipt"), Mapping):
            raise WebActivationError("activation rollback publication lost its intent")
        intent = value["receipt"]
        identity = _file_identity_from_payload(intent.get("journal_identity"))
        restored = publication.postimages.get(path)
        if (
            publication.state != "published"
            or publication.journal_path != journal
            or identity is None
            or not _same_publication_journal_inode(publication.journal_identity, identity)
            or publication.parent_guards != parent_guards
            or dict(publication.preimages) != {path: expected_postimage}
            or set(publication.postimages) != {path}
            or not isinstance(restored, release_manager._AnchoredFileSnapshot)
            or restored.snapshot != preimage.snapshot
            or restored.sha256 != preimage.sha256
        ):
            raise WebActivationError("activation rollback publication postimage changed")
        if restored.snapshot.exists:
            record["rollback_postimage"] = _postimage_payload(path, restored, parent_guards)
        else:
            record.pop("rollback_postimage", None)
        value["state"] = "published"
        value["receipt"] = _release_publication_receipt_payload(publication)
        if checkpoint is not None:
            checkpoint()

    def checkpoint_recovered(publication: release_manager.PublicationTransactionReceipt) -> None:
        # A callback exception can make the inner inverse compensate locally
        # back to B.  Keep that B's new exact identity durable before the inner
        # journal is retired, using the same checks as restart recovery.
        value = record.get("rollback_publication")
        if not isinstance(value, dict) or not isinstance(value.get("receipt"), Mapping):
            raise WebActivationError("activation rollback recovery lost its intent")
        intent = value["receipt"]
        identity = _file_identity_from_payload(intent.get("journal_identity"))
        before_images = {path: _release_publication_snapshot_payload(path, expected_postimage)}
        if (
            value.get("schema") != "termdeck.web-activation-file-rollback.v1"
            or value.get("state") not in {"intent", "published"}
            or intent.get("state") != value["state"]
            or intent.get("journal_path") != str(journal)
            or identity is None
            or _release_publication_parent_guards_from_payload(intent.get("parents")) != parent_guards
            or _release_publication_image_map(intent, "preimages") != before_images
            or not _outer_postimage_matches_release_image(path, record.get("postimage"), before_images[path])
        ):
            raise WebActivationError("activation rollback recovery intent changed")
        _checkpoint_rollback_recovery(
            record, publication, journal=journal, journal_identity=identity,
            parent_guards=parent_guards, before_images=before_images, checkpoint=checkpoint,
        )

    try:
        release_manager._publish_files_transactionally(
            ((
                path,
                preimage.snapshot.content if preimage.snapshot.exists else None,
                preimage.snapshot.mode,
                "activation rollback target",
            ),),
            parent_guards=parent_guards,
            expected_preimages={path: expected_postimage},
            journal_path=journal,
            before_publish=checkpoint_intent,
            after_publish=checkpoint_restored,
            after_recovery=checkpoint_recovered,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc
    return "restored"


def _strict_json_object(content: bytes, *, label: str) -> dict[str, object]:
    def no_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise WebActivationError(f"{label} has duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=no_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebActivationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WebActivationError(f"{label} is not a JSON object")
    return value


def _runtime_receipt_identity(value: object, *, label: str) -> release_manager._FileIdentity:
    if (
        not isinstance(value, list)
        or len(value) != len(_FILE_IDENTITY_FIELDS)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise WebActivationError(f"{label} identity is invalid")
    payload = dict(zip(_FILE_IDENTITY_FIELDS, value, strict=True))
    identity = _file_identity_from_payload(payload)
    if identity is None:
        raise WebActivationError(f"{label} identity is invalid")
    return identity


def _lower_hex(value: object, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _runtime_receipt_leaf_identity(value: object) -> tuple[int, int, int, int]:
    """Parse the immutable receipt identity committed before its write."""

    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise WebActivationError("LAN runtime preparation receipt identity is invalid")
    device, inode, mode, nlink = value
    if device < 0 or inode <= 0 or mode != 0o644 or nlink != 1:
        raise WebActivationError("LAN runtime preparation receipt identity is invalid")
    return device, inode, mode, nlink


def _runtime_private_ipv4(value: object) -> str:
    if not isinstance(value, str):
        raise WebActivationError("LAN runtime preparation LAN address is invalid")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise WebActivationError("LAN runtime preparation LAN address is invalid") from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or str(address) != value
        or not any(address in network for network in _RFC1918_NETWORKS)
    ):
        raise WebActivationError("LAN runtime preparation LAN address is invalid")
    return value


def _runtime_allowed_hosts(lan_ip: str) -> bytes:
    """Mirror the preparer's exact map payload, not merely its digest."""

    return (
        "localhost 1;\n"
        "127.0.0.1 1;\n"
        "termdeck.local 1;\n"
        "max.local 1;\n"
        f"{lan_ip} 1;\n"
        "maxchat.semiotech.org 1;\n"
        "semiotech.org 1;\n"
        "termdeck.semiotech.org 1;\n"
    ).encode("ascii")


def _runtime_https_port(config: bytes) -> int:
    """Use the same one-listen TLS parse as the descriptor-rooted producer."""

    try:
        text = config.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebActivationError("LAN runtime preparation nginx input is invalid") from exc
    listeners = re.findall(
        r"^\s*listen\s+[^;:]+:(\d+)\s+ssl\s*;\s*$",
        text,
        re.MULTILINE,
    )
    if len(listeners) != 1:
        raise WebActivationError("LAN runtime preparation nginx input has no unique TLS port")
    port = int(listeners[0])
    if not 1 <= port <= 65535:
        raise WebActivationError("LAN runtime preparation nginx input has an invalid TLS port")
    return port


def _capture_runtime_preparer_sources(
    paths: ActivationPaths,
) -> dict[str, tuple[
    Path,
    release_manager._AnchoredFileSnapshot,
    release_manager._PublicationParentGuards,
]]:
    """Capture every producer input through one stable set of parent FDs."""

    targets = {
        relative: _publication_path(
            paths.runtime / suffix,
            label=f"LAN runtime preparation source {relative}",
        )
        for relative, suffix in RUNTIME_PREPARER_SOURCE_PATHS.items()
    }
    if set(targets) != RUNTIME_PREPARER_SOURCE_FILES:
        raise WebActivationError("LAN runtime preparation source contract is invalid")
    guards = _capture_parent_guards(tuple(targets.values()))
    try:
        raw = release_manager._capture_anchored_files_safely(
            tuple(
                (path, f"LAN runtime preparation source {relative}")
                for relative, path in targets.items()
            ),
            parent_guards=guards,
        )
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc
    return {
        relative: (path, raw[path], guards)
        for relative, path in targets.items()
    }


def _assert_runtime_capture_current(
    path: Path,
    capture: release_manager._AnchoredFileSnapshot,
    parent_guards: release_manager._PublicationParentGuards,
    *,
    label: str,
) -> None:
    """Reject a leaf or ancestor swap after producer receipt validation."""

    _path, current = _capture_anchored(
        path,
        label=label,
        parent_guards=parent_guards,
    )
    if current != capture:
        raise WebActivationError(f"{label} changed while validating LAN preparation")


def _runtime_key_directory_identity(
    info: os.stat_result,
    *,
    label: str,
) -> tuple[int, int, int, int]:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_ino <= 0
        or info.st_nlink <= 0
    ):
        raise WebActivationError(f"LAN runtime producer key parent is invalid: {label}")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_uid),
    )


def _open_runtime_key_parent(
    parent: Path,
) -> tuple[int, tuple[tuple[int, int, int, int], ...]]:
    """Open the producer-key parent with the producer's no-follow walk."""

    target = _publication_path(parent, label="LAN runtime producer key parent")
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not isinstance(directory, int) or not directory or not isinstance(nofollow, int) or not nofollow:
        raise WebActivationError("LAN runtime producer key requires descriptor no-follow support")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open("/", flags)
    except OSError as exc:
        raise WebActivationError("LAN runtime producer key parent cannot be opened") from exc
    try:
        chain: list[tuple[int, int, int, int]] = [
            _runtime_key_directory_identity(os.fstat(descriptor), label="/")
        ]
        for component in target.parts[1:]:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise WebActivationError("LAN runtime producer key parent cannot be inspected") from exc
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise WebActivationError("LAN runtime producer key parent is not a real directory")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise WebActivationError("LAN runtime producer key parent cannot be opened") from exc
            try:
                opened = _runtime_key_directory_identity(
                    os.fstat(child), label=str(target)
                )
                named = _runtime_key_directory_identity(before, label=str(target))
                after = _runtime_key_directory_identity(
                    os.stat(component, dir_fd=descriptor, follow_symlinks=False),
                    label=str(target),
                )
                if opened != named or after != opened:
                    raise WebActivationError("LAN runtime producer key parent changed while opening")
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            chain.append(opened)
        return descriptor, tuple(chain)
    except Exception:
        os.close(descriptor)
        raise


def _runtime_tls_directory_identity(
    value: object,
    *,
    label: str,
) -> tuple[int, int, int, int]:
    """Parse the preparer's exact directory identity shape."""

    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise WebActivationError(f"LAN runtime preparation {label} is invalid")
    device, inode, mode, uid = value
    if device < 0 or inode <= 0 or mode != 0o700 or uid < 0:
        raise WebActivationError(f"LAN runtime preparation {label} is invalid")
    return device, inode, mode, uid


def _runtime_tls_leaf_identity(
    info: os.stat_result,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int, int]:
    """Return a no-follow TLS leaf identity matching the preparer contract."""

    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_ino <= 0
        or info.st_nlink != 1
        or info.st_size < 0
    ):
        raise WebActivationError(f"LAN runtime preparation {label} is unsafe")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _runtime_tls_manifest(
    value: object,
    *,
    label: str,
) -> dict[str, dict[str, object]]:
    """Validate the TLS candidate manifest bound by producer attestation."""

    if (
        not isinstance(value, Mapping)
        or not RUNTIME_TLS_REQUIRED_LEAVES.issubset(value)
        or set(value) - RUNTIME_TLS_ALLOWED_LEAVES
    ):
        raise WebActivationError(f"LAN runtime preparation {label} is invalid")
    result: dict[str, dict[str, object]] = {}
    for name in sorted(value):
        item = value[name]
        if not isinstance(item, Mapping) or set(item) != {"identity", "mode", "sha256"}:
            raise WebActivationError(f"LAN runtime preparation {label} is invalid")
        identity = _runtime_receipt_identity(
            item.get("identity"), label=f"LAN runtime {label}/{name}"
        )
        mode = item.get("mode")
        digest = item.get("sha256")
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode != identity.mode
            or (name in RUNTIME_TLS_REQUIRED_MODES and mode != RUNTIME_TLS_REQUIRED_MODES[name])
            or not _lower_hex(digest, length=64)
        ):
            raise WebActivationError(f"LAN runtime preparation {label} is invalid")
        result[name] = {
            "identity": list(item["identity"]),
            "mode": mode,
            "sha256": digest,
        }
    return result


def _parse_runtime_tls_publication(value: object) -> dict[str, object]:
    """Parse the signed TLS-generation shape without touching its live tree."""

    if not isinstance(value, Mapping) or set(value) != {"generation", "current", "prior"}:
        raise WebActivationError("LAN runtime preparation TLS publication is invalid")
    generation = value.get("generation")
    if not _lower_hex(generation, length=32):
        raise WebActivationError("LAN runtime preparation TLS generation is invalid")
    current = value.get("current")
    if (
        not isinstance(current, Mapping)
        or set(current) != {"path", "identity", "manifest"}
        or current.get("path") != RUNTIME_TLS_CURRENT_PATH
    ):
        raise WebActivationError("LAN runtime preparation current TLS publication is invalid")
    current_identity = _runtime_tls_directory_identity(
        current.get("identity"), label="current TLS identity"
    )
    current_manifest = _runtime_tls_manifest(
        current.get("manifest"), label="current TLS manifest"
    )

    prior = value.get("prior")
    normalized_prior: dict[str, object] | None = None
    if prior is not None:
        if (
            not isinstance(prior, Mapping)
            or set(prior) != {"journal", "journal_identity", "path", "identity", "manifest"}
            or prior.get("journal") != RUNTIME_TLS_JOURNAL_PREFIX + generation
            or prior.get("path") != RUNTIME_TLS_PRIOR_PATH
        ):
            raise WebActivationError("LAN runtime preparation prior TLS publication is invalid")
        normalized_prior = {
            "journal": prior["journal"],
            "journal_identity": list(_runtime_tls_directory_identity(
                prior.get("journal_identity"), label="TLS journal identity"
            )),
            "path": RUNTIME_TLS_PRIOR_PATH,
            "identity": list(_runtime_tls_directory_identity(
                prior.get("identity"), label="prior TLS identity"
            )),
            "manifest": _runtime_tls_manifest(
                prior.get("manifest"), label="prior TLS manifest"
            ),
        }
    return {
        "generation": generation,
        "current": {
            "path": RUNTIME_TLS_CURRENT_PATH,
            "identity": list(current_identity),
            "manifest": current_manifest,
        },
        "prior": normalized_prior,
    }


def _runtime_tls_publication(
    paths: ActivationPaths,
    value: object,
) -> dict[str, object]:
    """Verify the signed TLS generation is still the public descriptor tree.

    The LAN preparer exchanges a directory, so treating it as an ordinary
    path-based activation preimage would lose the no-follow and exact-identity
    guarantees used for the receipt outputs.  This check binds the full
    descriptor-observed TLS directory and every manifest leaf while the build
    receipt is still an authenticated public leaf.
    """

    normalized = _parse_runtime_tls_publication(value)
    generation = normalized["generation"]
    current = normalized["current"]
    if not isinstance(current, Mapping):  # defensive narrowing
        raise WebActivationError("LAN runtime preparation current TLS publication is invalid")
    current_identity = _runtime_tls_directory_identity(
        current.get("identity"), label="current TLS identity"
    )
    current_manifest = _runtime_tls_manifest(
        current.get("manifest"), label="current TLS manifest"
    )

    runtime_fd, chain = _open_runtime_key_parent(paths.runtime)
    tls_fd = -1
    held_leaves: dict[str, tuple[int, tuple[int, int, int, int, int, int, int]]] = {}
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
            raise WebActivationError("LAN runtime TLS validation requires descriptor no-follow support")
        try:
            named_before = os.stat(
                RUNTIME_TLS_CURRENT_PATH, dir_fd=runtime_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise WebActivationError("LAN runtime TLS directory is unavailable") from exc
        named_identity = _runtime_key_directory_identity(
            named_before, label="LAN runtime TLS directory"
        )
        if named_identity != current_identity:
            raise WebActivationError("LAN runtime TLS directory changed before receipt adoption")
        try:
            tls_fd = os.open(RUNTIME_TLS_CURRENT_PATH, directory_flags, dir_fd=runtime_fd)
        except OSError as exc:
            raise WebActivationError("LAN runtime TLS directory is unavailable") from exc
        opened_before = os.fstat(tls_fd)
        if _runtime_key_directory_identity(
            opened_before, label="LAN runtime TLS directory"
        ) != current_identity:
            raise WebActivationError("LAN runtime TLS directory changed while opening")
        initial_directory_times = (int(opened_before.st_mtime_ns), int(opened_before.st_ctime_ns))
        names_before = set(os.listdir(tls_fd))
        if names_before != set(current_manifest):
            raise WebActivationError("LAN runtime TLS directory inventory changed before receipt adoption")

        def verify_tls_leaves(*, phase: str) -> None:
            """Read and hash every signed leaf from its no-follow FD."""

            for name, entry in current_manifest.items():
                expected = _runtime_receipt_identity(
                    entry["identity"], label=f"LAN runtime current TLS manifest/{name}"
                )
                digest = entry.get("sha256")
                if not _lower_hex(digest, length=64):  # narrowed by parser; keep local proof explicit
                    raise WebActivationError(f"LAN runtime TLS manifest digest is invalid: {name}")
                try:
                    leaf_before = os.stat(name, dir_fd=tls_fd, follow_symlinks=False)
                except OSError as exc:
                    raise WebActivationError(f"LAN runtime TLS leaf is unavailable: {name}") from exc
                expected_identity = (
                    expected.device,
                    expected.inode,
                    expected.mode,
                    expected.nlink,
                    expected.size,
                    expected.mtime_ns,
                    expected.ctime_ns,
                )
                if _runtime_tls_leaf_identity(leaf_before, label=f"TLS leaf {name}") != expected_identity:
                    raise WebActivationError(f"LAN runtime TLS leaf changed {phase}: {name}")
                try:
                    held = held_leaves.get(name)
                    if held is None:
                        leaf_fd = os.open(
                            name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=tls_fd,
                        )
                        held_leaves[name] = (leaf_fd, expected_identity)
                    else:
                        leaf_fd, _held_identity = held
                        os.lseek(leaf_fd, 0, os.SEEK_SET)
                    if _runtime_tls_leaf_identity(
                        os.fstat(leaf_fd), label=f"TLS leaf {name}"
                    ) != expected_identity:
                        raise WebActivationError(f"LAN runtime TLS leaf changed while opening: {name}")
                    chunks: list[bytes] = []
                    remaining = expected.size
                    while remaining:
                        block = os.read(leaf_fd, min(1024 * 1024, remaining))
                        if not block:
                            raise WebActivationError(f"LAN runtime TLS leaf shortened while reading: {name}")
                        chunks.append(block)
                        remaining -= len(block)
                    if os.read(leaf_fd, 1):
                        raise WebActivationError(f"LAN runtime TLS leaf grew while reading: {name}")
                    if hashlib.sha256(b"".join(chunks)).hexdigest() != digest:
                        raise WebActivationError(f"LAN runtime TLS leaf digest changed {phase}: {name}")
                    if _runtime_tls_leaf_identity(
                        os.fstat(leaf_fd), label=f"TLS leaf {name}"
                    ) != expected_identity or _runtime_tls_leaf_identity(
                        os.stat(name, dir_fd=tls_fd, follow_symlinks=False),
                        label=f"TLS leaf {name}",
                    ) != expected_identity:
                        raise WebActivationError(f"LAN runtime TLS leaf changed while reading: {name}")
                except OSError as exc:
                    raise WebActivationError(f"LAN runtime TLS leaf is unavailable: {name}") from exc

        verify_tls_leaves(phase="before receipt adoption")

        named_after = os.stat(
            RUNTIME_TLS_CURRENT_PATH, dir_fd=runtime_fd, follow_symlinks=False
        )
        opened_after = os.fstat(tls_fd)
        if (
            _runtime_key_directory_identity(named_after, label="LAN runtime TLS directory")
            != current_identity
            or _runtime_key_directory_identity(opened_after, label="LAN runtime TLS directory")
            != current_identity
            or (int(opened_after.st_mtime_ns), int(opened_after.st_ctime_ns)) != initial_directory_times
            or set(os.listdir(tls_fd)) != names_before
        ):
            raise WebActivationError("LAN runtime TLS directory changed during receipt adoption")
        # The directory fence alone does not prove the contents of a leaf
        # which an attacker overwrote in place.  Re-read the complete signed
        # manifest after that fence, then take a final directory fence.
        verify_tls_leaves(phase="after receipt directory fence")
        named_final = os.stat(
            RUNTIME_TLS_CURRENT_PATH, dir_fd=runtime_fd, follow_symlinks=False
        )
        opened_final = os.fstat(tls_fd)
        if (
            _runtime_key_directory_identity(named_final, label="LAN runtime TLS directory")
            != current_identity
            or _runtime_key_directory_identity(opened_final, label="LAN runtime TLS directory")
            != current_identity
            or (int(opened_final.st_mtime_ns), int(opened_final.st_ctime_ns)) != initial_directory_times
            or set(os.listdir(tls_fd)) != names_before
        ):
            raise WebActivationError("LAN runtime TLS directory changed after receipt validation")

        # Keep every authenticated leaf open through the public ancestor
        # fence.  A directory-only fence cannot detect an in-place write
        # after the last hash pass, and a held descriptor alone cannot prove
        # that its inode is still the public leaf at the signed name.
        fresh_runtime_fd, fresh_chain = _open_runtime_key_parent(paths.runtime)
        try:
            if fresh_chain != chain:
                raise WebActivationError("LAN runtime TLS parent changed during receipt adoption")
        finally:
            os.close(fresh_runtime_fd)
        named_tls = os.stat(
            RUNTIME_TLS_CURRENT_PATH, dir_fd=runtime_fd, follow_symlinks=False
        )
        opened_tls = os.fstat(tls_fd)
        if (
            _runtime_key_directory_identity(named_tls, label="LAN runtime TLS directory")
            != current_identity
            or _runtime_key_directory_identity(opened_tls, label="LAN runtime TLS directory")
            != current_identity
            or (int(opened_tls.st_mtime_ns), int(opened_tls.st_ctime_ns)) != initial_directory_times
            or set(os.listdir(tls_fd)) != names_before
        ):
            raise WebActivationError("LAN runtime TLS directory changed after parent validation")
        for name, (leaf_fd, expected_identity) in held_leaves.items():
            try:
                descriptor_identity = _runtime_tls_leaf_identity(
                    os.fstat(leaf_fd), label=f"TLS leaf {name}"
                )
                named_identity = _runtime_tls_leaf_identity(
                    os.stat(name, dir_fd=tls_fd, follow_symlinks=False),
                    label=f"TLS leaf {name}",
                )
            except OSError as exc:
                raise WebActivationError(f"LAN runtime TLS leaf is unavailable: {name}") from exc
            if descriptor_identity != expected_identity or named_identity != expected_identity:
                raise WebActivationError(f"LAN runtime TLS leaf changed after receipt validation: {name}")
    finally:
        for leaf_fd, _expected_identity in held_leaves.values():
            os.close(leaf_fd)
        if tls_fd >= 0:
            os.close(tls_fd)
        os.close(runtime_fd)
    return normalized


def _runtime_attestation_config(paths: ActivationPaths) -> runtime_attestation.DaemonConfig:
    """Use the installed root/preparer boundary without an owner-UID fallback."""

    config = runtime_attestation.production_config()
    if (
        paths.runtime != service_wrappers.CANONICAL_RUNTIME_ROOT
        and _publication_path(paths.runtime / "build-receipt.json", label="LAN runtime receipt")
        != config.runtime_receipt_path
    ):
        raise WebActivationError("LAN runtime receipt is outside the attested production runtime")
    # In production the signature binds the private producer receipt.  The
    # broker separately binds its unchanged bytes to the consumer publication;
    # never substitute the consumer pathname into the signed request.
    return config


def _validate_runtime_preparer_receipt(
    paths: ActivationPaths,
    captures: Mapping[str, tuple[
        Path,
        release_manager._AnchoredFileSnapshot,
        release_manager._PublicationParentGuards,
    ]],
    *,
    consume: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Authenticate the producer and retain its receipt through outer checkpoints."""

    try:
        with runtime_attestation.open_verified_runtime_receipt(
            config=_runtime_attestation_config(paths),
        ) as lease:
            captured = captures["build-receipt.json"][1]
            if captured.identity != _runtime_receipt_identity(
                list(lease.receipt_identity), label="attested runtime receipt",
            ):
                raise WebActivationError("LAN runtime preparation receipt identity changed")
            if _strict_json_object(captured.snapshot.content, label="LAN runtime receipt") != lease.receipt:
                raise WebActivationError("LAN runtime preparation receipt changed during attestation")
            result = _validate_attested_runtime_preparer_receipt(paths, captures)
            lease.revalidate_receipt()
            if consume is not None:
                consume(result)
            lease.revalidate_receipt()
            for relative, (path, capture, guards) in captures.items():
                _assert_runtime_capture_current(
                    path, capture, guards, label=f"generated runtime target {relative}",
                )
            return result
    except runtime_attestation.AttestationError as exc:
        raise WebActivationError(f"LAN runtime producer attestation is invalid: {exc}") from exc


def _validate_attested_runtime_preparer_receipt(
    paths: ActivationPaths,
    captures: Mapping[str, tuple[
        Path,
        release_manager._AnchoredFileSnapshot,
        release_manager._PublicationParentGuards,
    ]],
) -> dict[str, object]:
    """Check the signed generation against exact live descriptor captures."""

    receipt_path, receipt_capture, _receipt_guards = captures["build-receipt.json"]
    if (
        not receipt_capture.snapshot.exists
        or receipt_capture.identity is None
        or receipt_capture.snapshot.mode != 0o644
        or receipt_capture.identity.nlink != 1
    ):
        raise WebActivationError("LAN preparation did not publish a safe runtime receipt")
    receipt = _strict_json_object(
        receipt_capture.snapshot.content,
        label="LAN runtime preparation receipt",
    )
    expected_top = {
        "files", "https_port", "lan_ip", "ok", "schema", "runtime_publication",
        "producer_attestation", "tls_publication",
    }
    if set(receipt) != expected_top or receipt.get("schema") != RUNTIME_PUBLICATION_SCHEMA:
        raise WebActivationError("LAN runtime preparation receipt schema is invalid")
    if receipt.get("ok") is not True:
        raise WebActivationError("LAN runtime preparation receipt did not succeed")
    port = receipt.get("https_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WebActivationError("LAN runtime preparation receipt port is invalid")
    lan_ip = _runtime_private_ipv4(receipt.get("lan_ip"))
    source_files = receipt.get("files")
    if (
        not isinstance(source_files, Mapping)
        or set(source_files) != RUNTIME_PREPARER_SOURCE_FILES
        or any(not _lower_hex(digest, length=64) for digest in source_files.values())
    ):
        raise WebActivationError("LAN runtime preparation receipt source binding is invalid")
    publication = receipt.get("runtime_publication")
    if not isinstance(publication, Mapping) or set(publication) != {"generation", "outputs"}:
        raise WebActivationError("LAN runtime preparation publication binding is invalid")
    generation = publication.get("generation")
    if not _lower_hex(generation, length=32):
        raise WebActivationError("LAN runtime preparation generation is invalid")
    outputs = publication.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != set(RUNTIME_PREPARER_OUTPUTS):
        raise WebActivationError("LAN runtime preparation output binding is invalid")
    for relative in RUNTIME_PREPARER_OUTPUTS:
        item = outputs.get(relative)
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "mode", "identity"}:
            raise WebActivationError(f"LAN runtime preparation output binding is invalid: {relative}")
        path, capture, _guards = captures[relative]
        if (
            item.get("path") != relative
            or not capture.snapshot.exists
            or capture.identity is None
            or capture.identity.nlink != 1
            or capture.snapshot.mode != 0o600
        ):
            raise WebActivationError(f"LAN runtime preparation output is unavailable: {relative}")
        digest = item.get("sha256")
        if (
            not _lower_hex(digest, length=64)
            or hashlib.sha256(capture.snapshot.content).hexdigest() != digest
        ):
            raise WebActivationError(f"LAN runtime preparation output digest is invalid: {relative}")
        mode = item.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode != capture.snapshot.mode:
            raise WebActivationError(f"LAN runtime preparation output mode is invalid: {relative}")
        if _runtime_receipt_identity(item.get("identity"), label=f"LAN runtime {relative}") != capture.identity:
            raise WebActivationError(f"LAN runtime preparation output identity is invalid: {relative}")
        # Keep ``path`` referenced so static analysis does not mistake the
        # capture association for a receipt-only assertion.
        if path != _publication_path(path, label=f"generated runtime target {relative}"):
            raise WebActivationError(f"LAN runtime preparation output path is invalid: {relative}")

    current_path, current_capture, _current_guards = captures["current-lan-ip"]
    map_path, map_capture, _map_guards = captures["nginx/allowed-hosts.map"]
    if current_capture.snapshot.content != (lan_ip + "\n").encode("ascii"):
        raise WebActivationError("LAN runtime preparation current LAN address is invalid")
    if map_capture.snapshot.content != _runtime_allowed_hosts(lan_ip):
        raise WebActivationError("LAN runtime preparation host map is invalid")
    # Keep these names in scope so the exact capture associations remain
    # visible to static analysis and future reviewer inspection.
    if current_path.name != "current-lan-ip" or map_path.name != "allowed-hosts.map":
        raise WebActivationError("LAN runtime preparation output paths are invalid")

    tls_publication = _runtime_tls_publication(paths, receipt.get("tls_publication"))
    source_captures = _capture_runtime_preparer_sources(paths)
    for relative in sorted(RUNTIME_PREPARER_SOURCE_FILES):
        _path, capture, _guards = source_captures[relative]
        if (
            not capture.snapshot.exists
            or capture.identity is None
            or capture.identity.nlink != 1
            or hashlib.sha256(capture.snapshot.content).hexdigest() != source_files[relative]
        ):
            raise WebActivationError(f"LAN runtime preparation source changed: {relative}")

    nginx_capture = source_captures["nginx/nginx.conf"][1]
    if _runtime_https_port(nginx_capture.snapshot.content) != port:
        raise WebActivationError("LAN runtime preparation receipt port does not match nginx input")

    # The broker signature authenticates the receipt core.  Exact anchored
    # observations additionally require the same public leaves and parents.
    for relative, (path, capture, guards) in captures.items():
        _assert_runtime_capture_current(
            path,
            capture,
            guards,
            label=f"generated runtime target {relative}",
        )
    for relative, (path, capture, guards) in source_captures.items():
        _assert_runtime_capture_current(
            path,
            capture,
            guards,
            label=f"LAN runtime preparation source {relative}",
        )
    if _runtime_tls_publication(paths, receipt["tls_publication"]) != tls_publication:
        raise WebActivationError("LAN runtime TLS publication changed while validating receipt")
    return tls_publication


def _capture_generated_runtime_postimages(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
    allow_absent: bool = False,
) -> None:
    """Adopt only outputs bound by the preparer's final v2 receipt.

    The preparer publishes its receipt last.  Capture all three leaves first,
    validate that receipt against their exact post-rename identities, then
    append each outer postimage checkpoint.  A restart can repeat this process
    if it crashed after preparation but before any outer checkpoint.
    """

    # The producer is private to termdeck-preparer.  Activation observes the
    # consumer tree, whose inodes differ from the signed producer generation.
    # The broker joins those two observations without changing signed bytes.
    if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
        _capture_generated_runtime_postimages_via_broker(
            paths,
            tx,
            preimages,
            checkpoint=checkpoint,
            allow_absent=allow_absent,
        )
        return

    captured: dict[str, tuple[
        dict[str, object],
        Path,
        release_manager._AnchoredFileSnapshot,
        release_manager._PublicationParentGuards,
        release_manager._AnchoredFileSnapshot,
    ]] = {}
    changed = False
    for relative in GENERATED_RUNTIME:
        key = "generated:" + relative
        value = preimages.get(key)
        if not isinstance(value, dict):
            raise WebActivationError(f"generated runtime publication lacks preimage: {relative}")
        expected_path = _publication_path(
            paths.runtime / relative,
            label=f"generated runtime target {relative}",
        )
        path = _record_target(value)
        if path != expected_path:
            raise WebActivationError(f"generated runtime preimage path does not match target: {relative}")
        parent_guards = _record_parent_guards(value, path)
        preimage = _receipt_preimage(value, tx)
        _path, capture = _capture_anchored(
            path,
            label=f"generated runtime target {relative}",
            parent_guards=parent_guards,
        )
        if not isinstance(capture, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("invalid generated runtime capture")
        existing_postimage = value.get("postimage")
        if existing_postimage is not None:
            if not isinstance(existing_postimage, Mapping):
                raise WebActivationError(f"generated runtime postimage is invalid: {relative}")
            postimage_guards = _parent_guards_from_payload(
                existing_postimage.get("parent_guard"), path,
            )
            if postimage_guards != parent_guards:
                raise WebActivationError(
                    f"generated runtime postimage parent guard does not match preimage: {relative}"
                )
            _expected_postimage(path, capture, existing_postimage)
        elif not _same_preimage_inode(capture, preimage):
            changed = True
        captured[relative] = (value, path, capture, parent_guards, preimage)

    # An exception path with no visible preparation writes needs no authority
    # to undo them.  Any visible changed output, including only one leaf,
    # requires the preparer's complete final receipt.
    if allow_absent and not changed:
        return

    receipt_captures = {
        relative: (path, capture, guards)
        for relative, (_value, path, capture, guards, _preimage) in captured.items()
    }
    def adopt(tls_publication: dict[str, object]) -> None:
        # Persist the authenticated TLS generation before any individual outer
        # output postimage.  A failure after synchronous preparation can then
        # restore the TLS generation before receipt/map/current A, instead of
        # leaving B TLS paired with an A runtime receipt.
        receipt_record = captured["build-receipt.json"][0]
        existing_tls = receipt_record.get("tls_publication")
        if existing_tls is None:
            receipt_record["tls_publication"] = tls_publication
            checkpoint("generated:tls-publication")
        elif existing_tls != tls_publication:
            raise WebActivationError("generated runtime TLS publication changed after adoption")

        for relative in GENERATED_RUNTIME:
            value, path, capture, parent_guards, _preimage = captured[relative]
            if value.get("postimage") is not None:
                continue
            value["postimage"] = _postimage_payload(path, capture, parent_guards)
            checkpoint("generated:" + relative)

    _validate_runtime_preparer_receipt(paths, receipt_captures, consume=adopt)


def _runtime_broker_postimage(
    record: Mapping[str, object],
    path: Path,
    claim: Mapping[str, object],
    *,
    expected_mode: int,
) -> dict[str, object]:
    """Convert a root-broker leaf claim into an outer activation postimage."""

    if set(claim) not in ({"identity", "sha256"}, {"path", "sha256", "mode", "identity"}):
        raise WebActivationError(f"LAN broker runtime claim is invalid: {path}")
    identity = _runtime_receipt_identity(claim.get("identity"), label=f"LAN broker {path}")
    digest = claim.get("sha256")
    if (
        not _lower_hex(digest, length=64)
        or identity.mode != expected_mode
        or identity.nlink != 1
        or (
            set(claim) == {"path", "sha256", "mode", "identity"}
            and claim.get("mode") != expected_mode
        )
    ):
        raise WebActivationError(f"LAN broker runtime claim is invalid: {path}")
    parent_guards = _record_parent_guards(record, path)
    return {
        "sha256": digest,
        "size": identity.size,
        "mode": expected_mode,
        "identity": _file_identity_payload(identity),
        "parent_guard": _parent_guard_payload(parent_guards, path),
    }


def _broker_runtime_receipt(paths: ActivationPaths) -> tuple[
    dict[str, object], release_manager._FileIdentity, dict[str, object]
]:
    """Authenticate producer content and the separate consumer observation."""

    try:
        result = service_wrappers._request_lan_preparation(
            restore_mode="verify",
            runtime_root=paths.runtime,
            timeout=90.0,
        )
    except service_wrappers.WrapperError as exc:
        raise WebActivationError("LAN runtime receipt broker observation failed") from exc
    if (
        not isinstance(result, Mapping)
        or set(result) != {"identity", "receipt", "sha256", "publication"}
        or not isinstance(result.get("receipt"), Mapping)
    ):
        raise WebActivationError("LAN runtime receipt broker result is invalid")
    receipt = dict(result["receipt"])
    publication = result["publication"]
    try:
        service_wrappers._validate_preparer_publication_result(publication, operation="verify")
    except service_wrappers.WrapperError as exc:
        raise WebActivationError("LAN runtime consumer publication is invalid") from exc
    identity = _runtime_receipt_identity(result.get("identity"), label="LAN broker receipt")
    digest = result.get("sha256")
    payload = runtime_attestation._canonical_json(receipt)
    if (
        identity.mode != 0o644
        or identity.nlink != 1
        or not _lower_hex(digest, length=64)
        or hashlib.sha256(payload).hexdigest() != digest
        or identity.size != len(payload)
        or publication["receipt"]["identity"] != result["identity"]
        or publication["producer_receipt_sha256"] != digest
    ):
        raise WebActivationError("LAN runtime receipt broker identity is invalid")
    try:
        runtime_attestation.verify_runtime_build_attestation(
            receipt,
            config=_runtime_attestation_config(paths),
        )
    except runtime_attestation.AttestationError as exc:
        raise WebActivationError("LAN runtime receipt broker attestation is invalid") from exc
    signed_runtime = receipt["runtime_publication"]
    signed_tls = _parse_runtime_tls_publication(receipt["tls_publication"])
    if publication["generation"] != signed_runtime["generation"]:
        raise WebActivationError("LAN runtime consumer generation differs from producer")

    def match_content(consumer: Mapping[str, object], producer: Mapping[str, object]) -> None:
        consumer_identity = _runtime_receipt_identity(consumer["identity"], label="consumer leaf")
        producer_identity = _runtime_receipt_identity(producer["identity"], label="producer leaf")
        if (
            consumer["sha256"] != producer["sha256"]
            or consumer["mode"] != producer["mode"]
            or consumer_identity.size != producer_identity.size
        ):
            raise WebActivationError("LAN runtime consumer content differs from producer")

    for relative in RUNTIME_PREPARER_OUTPUTS:
        match_content(publication["outputs"][relative], signed_runtime["outputs"][relative])
    manifest = publication["tls"]["manifest"]
    signed_manifest = signed_tls["current"]["manifest"]
    if set(manifest) != set(signed_manifest):
        raise WebActivationError("LAN runtime consumer TLS inventory differs from producer")
    for name in manifest:
        match_content(manifest[name], signed_manifest[name])
    return receipt, identity, dict(publication)


def _consumer_tls_publication(publication: Mapping[str, object]) -> dict[str, object]:
    """Use consumer identities for local observation, without editing a signature."""

    tls = publication["tls"]
    return _parse_runtime_tls_publication({
        "generation": publication["generation"],
        "current": {
            "path": tls["path"],
            "identity": tls["identity"],
            "manifest": {
                name: {key: claim[key] for key in ("identity", "mode", "sha256")}
                for name, claim in tls["manifest"].items()
            },
        },
        "prior": None,
    })


def _capture_restored_consumer_tls(
    paths: ActivationPaths, generation: str, producer_tls: Mapping[str, object]
) -> dict[str, object]:
    """Observe copied TLS while retaining the producer's authenticated content."""

    expected = _parse_runtime_tls_publication({
        "generation": generation, "current": producer_tls, "prior": None,
    })["current"]
    tls_path = _publication_path(paths.runtime / "tls", label="restored consumer TLS")
    info = tls_path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise WebActivationError("restored consumer TLS directory is unsafe")
    manifest = {}
    for name, producer_claim in expected["manifest"].items():
        path = tls_path / name
        guards = _capture_parent_guards((path,))
        _path, capture = _capture_anchored(path, label=f"restored consumer TLS {name}", parent_guards=guards)
        source = _runtime_receipt_identity(producer_claim["identity"], label="restored producer TLS")
        if (
            not capture.snapshot.exists or capture.identity is None
            or capture.identity.nlink != 1 or capture.identity.size != source.size
            or capture.snapshot.mode != producer_claim["mode"]
            or hashlib.sha256(capture.snapshot.content).hexdigest() != producer_claim["sha256"]
        ):
            raise WebActivationError(f"restored consumer TLS differs from producer: {name}")
        manifest[name] = {
            "identity": [getattr(capture.identity, field) for field in _FILE_IDENTITY_FIELDS],
            "mode": capture.snapshot.mode,
            "sha256": producer_claim["sha256"],
        }
    observed = {
        "generation": generation,
        "current": {"path": "tls", "identity": [info.st_dev, info.st_ino, 0o700, info.st_uid], "manifest": manifest},
        "prior": None,
    }
    return _runtime_tls_publication(paths, observed)


def _verify_consumer_receipt_tombstone(record: Mapping[str, object], *, replay: bool) -> None:
    path = _record_target(record)
    guards = _record_parent_guards(record, path)
    _path, current = _capture_anchored(path, label="consumer receipt after TLS inverse", parent_guards=guards)
    if current.snapshot.exists:
        restored = record.get("rollback_postimage")
        if not replay or not isinstance(restored, Mapping):
            raise WebActivationError("LAN runtime TLS rollback left an unowned consumer receipt")
        _expected_postimage(path, current, restored)


def _capture_generated_runtime_postimages_via_broker(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
    allow_absent: bool,
) -> None:
    """Adopt consumer LAN leaves bound to one authenticated producer generation."""

    receipt, receipt_identity, publication = _broker_runtime_receipt(paths)
    if not isinstance(publication, Mapping):
        raise WebActivationError("LAN broker runtime receipt lacks publication metadata")
    outputs = publication.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != set(RUNTIME_PREPARER_OUTPUTS):
        raise WebActivationError("LAN broker runtime receipt output metadata is invalid")
    claims: dict[str, Mapping[str, object]] = {
        relative: outputs[relative]
        for relative in RUNTIME_PREPARER_OUTPUTS
        if isinstance(outputs.get(relative), Mapping)
    }
    if set(claims) != set(RUNTIME_PREPARER_OUTPUTS):
        raise WebActivationError("LAN broker runtime receipt output metadata is invalid")
    receipt_record = preimages.get("generated:build-receipt.json")
    if not isinstance(receipt_record, dict):
        raise WebActivationError("generated runtime publication lacks receipt preimage")
    receipt_path = _record_target(receipt_record)
    expected_receipt_path = _publication_path(
        paths.runtime / "build-receipt.json", label="generated runtime receipt"
    )
    if receipt_path != expected_receipt_path:
        raise WebActivationError("generated runtime receipt preimage path is invalid")
    receipt_claim = publication["receipt"]
    postimages: dict[str, dict[str, object]] = {
        "build-receipt.json": _runtime_broker_postimage(
            receipt_record,
            receipt_path,
            receipt_claim,
            expected_mode=0o644,
        ),
    }
    changed = _file_identity_from_payload(receipt_record.get("preimage_identity")) != receipt_identity
    for relative in RUNTIME_PREPARER_OUTPUTS:
        record = preimages.get("generated:" + relative)
        if not isinstance(record, dict):
            raise WebActivationError(f"generated runtime publication lacks preimage: {relative}")
        path = _record_target(record)
        expected_path = _publication_path(paths.runtime / relative, label=f"generated runtime {relative}")
        if path != expected_path:
            raise WebActivationError(f"generated runtime preimage path is invalid: {relative}")
        if claims[relative].get("path") != relative:
            raise WebActivationError(f"LAN broker runtime claim path is invalid: {relative}")
        postimages[relative] = _runtime_broker_postimage(
            record,
            path,
            claims[relative],
            expected_mode=0o600,
        )
        changed = changed or (
            _file_identity_from_payload(record.get("preimage_identity"))
            != _runtime_receipt_identity(claims[relative].get("identity"), label=f"LAN broker {relative}")
        )
    # The broker has released its transaction lock.  Recheck the consumer
    # paths against its descriptor observations before recording postimages.
    def validate_consumer() -> None:
        for relative, expected in postimages.items():
            record = preimages["generated:" + relative]
            path = _record_target(record)
            guards = _record_parent_guards(record, path)
            _path, capture = _capture_anchored(
                path, label=f"LAN consumer {relative}", parent_guards=guards,
            )
            _expected_postimage(path, capture, expected)
        _runtime_tls_publication(paths, _consumer_tls_publication(publication))

    validate_consumer()
    if allow_absent and not changed:
        return
    existing_tls = receipt_record.get("tls_publication")
    tls_publication = receipt.get("tls_publication")
    if not isinstance(tls_publication, Mapping):
        raise WebActivationError("LAN broker runtime receipt TLS metadata is invalid")
    if existing_tls is None:
        receipt_record["tls_publication"] = dict(tls_publication)
        checkpoint("generated:tls-publication")
    elif existing_tls != tls_publication:
        raise WebActivationError("LAN broker runtime receipt TLS metadata changed")
    consumer_tls = _consumer_tls_publication(publication)
    existing_consumer_tls = receipt_record.get("consumer_tls_publication")
    if existing_consumer_tls is None:
        receipt_record["consumer_tls_publication"] = consumer_tls
        checkpoint("generated:consumer-tls-publication")
    elif existing_consumer_tls != consumer_tls:
        raise WebActivationError("LAN consumer TLS metadata changed after adoption")
    for relative in GENERATED_RUNTIME:
        record = preimages.get("generated:" + relative)
        if not isinstance(record, dict):
            raise WebActivationError(f"generated runtime publication lacks preimage: {relative}")
        expected = record.get("postimage")
        if expected is not None:
            if expected != postimages[relative]:
                raise WebActivationError(f"generated runtime postimage changed: {relative}")
            continue
        record["postimage"] = postimages[relative]
        checkpoint("generated:" + relative)
    validate_consumer()


def _runtime_inverse_leaf_claim(value: object, *, relative: str) -> dict[str, object]:
    """Parse one exact producer-reported output without treating it as authority."""

    if not isinstance(value, Mapping) or set(value) != {"identity", "sha256"}:
        raise WebActivationError(f"LAN runtime inverse leaf claim is invalid: {relative}")
    identity = _runtime_receipt_identity(value.get("identity"), label=f"inverse {relative}")
    expected_mode = 0o644 if relative == "build-receipt.json" else 0o600
    if identity.mode != expected_mode or identity.nlink != 1 or not _lower_hex(value.get("sha256"), length=64):
        raise WebActivationError(f"LAN runtime inverse leaf claim is invalid: {relative}")
    return {"identity": list(value["identity"]), "sha256": value["sha256"]}


def _parse_runtime_inverse_result(
    value: object,
    *,
    intent: Mapping[str, object],
    publication: Mapping[str, object] | None,
) -> dict[str, object]:
    """Require the complete inverse protocol and its signed B-to-A linkage."""

    fields = {"ok", "action", "generation", "receipt", "restored", "retained"}
    if isinstance(value, Mapping) and "consumer_publication" in value:
        fields.add("consumer_publication")
        _unwrap_broker_inverse({
            "producer": {key: item for key, item in value.items() if key != "consumer_publication"},
            "publication": value["consumer_publication"],
        })
    discovered = intent.get("mode") == "discover"
    if discovered:
        fields.add("discovered")
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("ok") is not True
        or discovered and value.get("discovered") is not True
        or not _lower_hex(value.get("generation"), length=32)
        or not discovered and value.get("generation") != intent.get("generation")
    ):
        raise WebActivationError("LAN runtime inverse result is invalid")
    generation = value["generation"]
    action = value.get("action")
    restored = value.get("restored")
    retained = value.get("retained")
    if not isinstance(restored, Mapping) or not isinstance(retained, Mapping):
        raise WebActivationError("LAN runtime inverse result is invalid")
    retained_fields = {"identity", "journal", "journal_identity", "manifest", "path"}
    if "outputs" in retained:
        retained_fields.add("outputs")
        outputs = retained["outputs"]
        if not isinstance(outputs, Mapping) or set(outputs) != set(RUNTIME_PREPARER_OUTPUTS):
            raise WebActivationError("LAN runtime inverse retained outputs are invalid")
        for relative, claim in outputs.items():
            _runtime_inverse_leaf_claim(claim, relative=relative)
    if set(retained) != retained_fields:
        raise WebActivationError("LAN runtime inverse retained TLS is invalid")
    retained_tls = {key: retained[key] for key in retained_fields if key != "outputs"}
    if action == "restored-runtime-generation":
        if set(restored) != {"outputs", "runtime_generation", "tls"} or not _lower_hex(restored.get("runtime_generation"), length=32):
            raise WebActivationError("LAN runtime inverse restored generation is invalid")
        outputs = restored.get("outputs")
        if not isinstance(outputs, Mapping) or set(outputs) != set(RUNTIME_PREPARER_OUTPUTS):
            raise WebActivationError("LAN runtime inverse restored outputs are invalid")
        for relative, claim in outputs.items():
            _runtime_inverse_leaf_claim(claim, relative=relative)
        receipt_claim = value.get("receipt")
        if (
            not isinstance(receipt_claim, Mapping)
            or set(receipt_claim) != {"path", "identity", "sha256"}
            or receipt_claim.get("path") != "build-receipt.json"
        ):
            raise WebActivationError("LAN runtime inverse restored receipt is invalid")
        _runtime_inverse_leaf_claim(
            {key: receipt_claim[key] for key in ("identity", "sha256")},
            relative="build-receipt.json",
        )
        restored_tls = restored["tls"]
    elif action == "restored-tls-generation" and not discovered:
        if value.get("receipt") != "absent" or "outputs" in retained:
            raise WebActivationError("LAN runtime TLS inverse result is invalid")
        restored_tls = restored
    else:
        raise WebActivationError("LAN runtime inverse did not restore a supported generation")
    parsed = _parse_runtime_tls_publication({
        "generation": generation,
        "current": restored_tls,
        "prior": retained_tls,
    })
    if publication is not None:
        prior = publication.get("prior")
        current = publication.get("current")
        if not isinstance(prior, Mapping) or not isinstance(current, Mapping):
            raise WebActivationError("LAN runtime TLS publication has no authenticated prior generation")
        expected_restored = {
            "path": RUNTIME_TLS_CURRENT_PATH,
            "identity": prior["identity"],
            "manifest": prior["manifest"],
        }
        expected_retained = {
            "path": RUNTIME_TLS_PRIOR_PATH,
            "journal": prior["journal"],
            "journal_identity": prior["journal_identity"],
            "identity": current["identity"],
            "manifest": current["manifest"],
        }
        if parsed["current"] != expected_restored or parsed["prior"] != expected_retained:
            raise WebActivationError("LAN runtime inverse does not match its authenticated B generation")
    return dict(value)


def _record_restored_runtime_generation(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    response: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
    replay: bool,
) -> None:
    """Authenticate public A and checkpoint every restored leaf under its lease."""

    if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
        _record_restored_runtime_generation_via_broker(
            paths,
            tx,
            preimages,
            response,
            checkpoint=checkpoint,
            replay=replay,
        )
        return

    restored = response["restored"]
    if not isinstance(restored, Mapping):
        raise WebActivationError("LAN runtime inverse restored generation is invalid")
    outputs = restored["outputs"]
    if not isinstance(outputs, Mapping):
        raise WebActivationError("LAN runtime inverse restored outputs are invalid")
    records: dict[str, dict[str, object]] = {}
    captures: dict[str, tuple[
        Path,
        release_manager._AnchoredFileSnapshot,
        release_manager._PublicationParentGuards,
    ]] = {}
    old_receipt: dict[str, object] | None = None
    for relative in GENERATED_RUNTIME:
        record = preimages.get("generated:" + relative)
        if not isinstance(record, dict):
            raise WebActivationError(f"LAN runtime inverse lacks preimage: {relative}")
        path = _record_target(record)
        if path != _publication_path(paths.runtime / relative, label=f"inverse target {relative}"):
            raise WebActivationError(f"LAN runtime inverse preimage target is invalid: {relative}")
        guards = _record_parent_guards(record, path)
        preimage = _receipt_preimage(record, tx)
        _path, capture = _capture_anchored(path, label=f"restored runtime target {relative}", parent_guards=guards)
        if not isinstance(capture, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("LAN runtime inverse capture is invalid")
        claim = response["receipt"] if relative == "build-receipt.json" else outputs[relative]
        if not isinstance(claim, Mapping):
            raise WebActivationError(f"LAN runtime inverse leaf claim is invalid: {relative}")
        if (
            not capture.snapshot.exists
            or capture.identity != _runtime_receipt_identity(claim.get("identity"), label=f"inverse {relative}")
            or hashlib.sha256(capture.snapshot.content).hexdigest() != claim.get("sha256")
            or not preimage.snapshot.exists
        ):
            raise WebActivationError(f"LAN runtime inverse output does not match its restored claim: {relative}")
        if relative == "build-receipt.json":
            old_receipt = _strict_json_object(preimage.snapshot.content, label="pre-activation runtime receipt")
        elif capture.snapshot.content != preimage.snapshot.content or capture.snapshot.mode != preimage.snapshot.mode:
            raise WebActivationError(f"LAN runtime inverse did not restore its activation preimage: {relative}")
        existing = record.get("rollback_postimage")
        if replay and not isinstance(existing, Mapping):
            raise WebActivationError(f"LAN runtime inverse replay lacks restored postimage: {relative}")
        if existing is not None:
            if not isinstance(existing, Mapping) or _parent_guards_from_payload(existing.get("parent_guard"), path) != guards:
                raise WebActivationError(f"LAN runtime inverse restored postimage is invalid: {relative}")
            _expected_postimage(path, capture, existing)
        records[relative] = record
        captures[relative] = (path, capture, guards)
    if old_receipt is None:
        raise WebActivationError("LAN runtime inverse lacks a prior runtime receipt")
    expected_a_tls = _parse_runtime_tls_publication(old_receipt.get("tls_publication"))["current"]
    if restored.get("tls") != expected_a_tls:
        raise WebActivationError("LAN runtime inverse TLS does not match the activation preimage")
    public_receipt = _strict_json_object(captures["build-receipt.json"][1].snapshot.content, label="restored runtime receipt")
    for field in ("files", "https_port", "lan_ip", "ok", "schema"):
        if public_receipt.get(field) != old_receipt.get(field):
            raise WebActivationError(f"LAN runtime inverse receipt changed its original {field}")
    public_runtime = public_receipt.get("runtime_publication")
    if not isinstance(public_runtime, Mapping) or public_runtime.get("generation") != restored.get("runtime_generation"):
        raise WebActivationError("LAN runtime inverse runtime generation does not match its receipt")

    def complete(tls_publication: dict[str, object]) -> None:
        if tls_publication.get("current") != restored.get("tls"):
            raise WebActivationError("LAN runtime inverse TLS does not match its attested receipt")
        if replay:
            return
        postimages = {
            relative: _postimage_payload(path, capture, guards)
            for relative, (path, capture, guards) in captures.items()
        }
        for relative, record in records.items():
            record["rollback_postimage"] = postimages[relative]
            record["rollback_restore"] = "restored"
        records["build-receipt.json"]["runtime_rollback"] = dict(response)
        checkpoint("rollback:generated-runtime")

    _validate_runtime_preparer_receipt(paths, captures, consume=complete)


def _record_restored_runtime_generation_via_broker(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
    response: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
    replay: bool,
) -> None:
    """Verify restored A and its current consumer copies before checkpointing."""

    restored = response.get("restored")
    if not isinstance(restored, Mapping):
        raise WebActivationError("LAN runtime inverse restored generation is invalid")
    consumer = response.get("consumer_publication")
    if not isinstance(consumer, Mapping):
        raise WebActivationError("LAN runtime inverse lacks consumer publication")
    public_receipt, _identity, publication = _broker_runtime_receipt(paths)
    if (
        public_receipt["runtime_publication"]["generation"] != restored.get("runtime_generation")
        or public_receipt["tls_publication"]["current"] != restored.get("tls")
        or publication["receipt"] != consumer.get("receipt")
        or publication["outputs"] != consumer.get("restored", {}).get("outputs")
        or publication["receipt"]["sha256"] != response["receipt"]["sha256"]
    ):
        raise WebActivationError("LAN runtime inverse differs from fresh broker verification")
    records: dict[str, dict[str, object]] = {}
    postimages: dict[str, dict[str, object]] = {}
    captures = {}
    for relative in GENERATED_RUNTIME:
        record = preimages.get("generated:" + relative)
        if not isinstance(record, dict):
            raise WebActivationError(f"LAN runtime inverse lacks preimage: {relative}")
        path = _record_target(record)
        expected_path = _publication_path(paths.runtime / relative, label=f"inverse target {relative}")
        if path != expected_path:
            raise WebActivationError(f"LAN runtime inverse preimage target is invalid: {relative}")
        claim = publication["receipt"] if relative == "build-receipt.json" else publication["outputs"][relative]
        postimage = _runtime_broker_postimage(
            record,
            path,
            claim,
            expected_mode=0o644 if relative == "build-receipt.json" else 0o600,
        )
        guards = _record_parent_guards(record, path)
        _path, capture = _capture_anchored(path, label=f"restored consumer {relative}", parent_guards=guards)
        _expected_postimage(path, capture, postimage)
        preimage = _receipt_preimage(record, tx)
        if not preimage.snapshot.exists:
            raise WebActivationError(f"LAN runtime inverse lacks activation preimage: {relative}")
        if relative != "build-receipt.json" and (
            preimage.snapshot.content != capture.snapshot.content
            or preimage.snapshot.mode != capture.snapshot.mode
        ):
            raise WebActivationError(f"LAN runtime inverse did not restore activation content: {relative}")
        existing = record.get("rollback_postimage")
        if replay:
            if not isinstance(existing, Mapping) or dict(existing) != postimage:
                raise WebActivationError(f"LAN runtime inverse replay lacks restored postimage: {relative}")
        elif existing is not None and dict(existing) != postimage:
            raise WebActivationError(f"LAN runtime inverse restored postimage changed: {relative}")
        records[relative] = record
        postimages[relative] = postimage
        captures[relative] = (path, capture, guards)

    receipt_record = records["build-receipt.json"]
    preimage = _receipt_preimage(receipt_record, tx)
    if not preimage.snapshot.exists:
        raise WebActivationError("LAN runtime inverse lacks a prior runtime receipt")
    try:
        old_receipt = _strict_json_object(
            preimage.snapshot.content,
            label="pre-activation runtime receipt",
        )
    except WebActivationError as exc:
        raise WebActivationError("LAN runtime inverse prior receipt is invalid") from exc
    for field in ("files", "https_port", "lan_ip", "ok", "schema"):
        if public_receipt.get(field) != old_receipt.get(field):
            raise WebActivationError(f"LAN runtime inverse receipt changed original {field}")
    old_tls = old_receipt.get("tls_publication")
    restored_tls = restored.get("tls")
    if not isinstance(old_tls, Mapping) or not isinstance(restored_tls, Mapping):
        raise WebActivationError("LAN runtime inverse TLS metadata is invalid")
    try:
        expected_tls = _parse_runtime_tls_publication(old_tls)["current"]
    except WebActivationError as exc:
        raise WebActivationError("LAN runtime inverse prior TLS metadata is invalid") from exc
    if restored_tls != expected_tls:
        raise WebActivationError("LAN runtime inverse TLS does not match its preimage")
    consumer_tls = _consumer_tls_publication(publication)
    _runtime_tls_publication(paths, consumer_tls)
    existing_tls = receipt_record.get("consumer_tls_rollback")
    if (replay or existing_tls is not None) and existing_tls != consumer_tls:
        raise WebActivationError("LAN runtime inverse consumer TLS changed after restoration")
    if not replay:
        for relative, record in records.items():
            record["rollback_postimage"] = postimages[relative]
            record["rollback_restore"] = "restored"
        receipt_record["runtime_rollback"] = dict(response)
        receipt_record["consumer_tls_rollback"] = consumer_tls
        checkpoint("rollback:generated-runtime")
    for relative, (path, capture, guards) in captures.items():
        _assert_runtime_capture_current(path, capture, guards, label=f"restored consumer {relative}")
    _runtime_tls_publication(paths, consumer_tls)


def _runtime_inverse_has_no_changes(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
) -> bool:
    """Prove an untouched A or entirely absent bootstrap before discovery."""

    if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
        # Verify private producer authority through the broker, then observe
        # the user-owned consumer paths without opening private producer data.
        try:
            broker_receipt, broker_identity, publication = _broker_runtime_receipt(paths)
        except WebActivationError:
            # An interrupted generation may be unverifiable precisely because
            # it needs the authenticated inverse.  This is no proof that A
            # survived: let the caller persist discovery intent and ask the
            # broker to recover.  An unavailable or invalid inverse still fails.
            return False
        receipt_record = preimages.get("generated:build-receipt.json")
        if not isinstance(receipt_record, Mapping):
            raise WebActivationError("LAN runtime inverse lacks receipt provenance")
        expected_receipt = _file_identity_from_payload(
            receipt_record.get("preimage_identity")
        )
        if expected_receipt is None or broker_identity != expected_receipt:
            return False
        outputs = publication.get("outputs") if isinstance(publication, Mapping) else None
        if not isinstance(outputs, Mapping) or set(outputs) != set(RUNTIME_PREPARER_OUTPUTS):
            raise WebActivationError("LAN runtime broker receipt outputs are invalid")
        for relative in RUNTIME_PREPARER_OUTPUTS:
            record = preimages.get("generated:" + relative)
            claim = outputs.get(relative)
            if not isinstance(record, Mapping) or not isinstance(claim, Mapping):
                raise WebActivationError(f"LAN runtime inverse lacks output provenance: {relative}")
            expected = _file_identity_from_payload(record.get("preimage_identity"))
            observed = _runtime_receipt_identity(
                claim.get("identity"), label=f"LAN broker unchanged {relative}"
            )
            if expected is None or expected != observed:
                return False
        for relative in GENERATED_RUNTIME:
            record = preimages["generated:" + relative]
            path = _record_target(record)
            if path != _publication_path(paths.runtime / relative, label=f"unchanged consumer {relative}"):
                raise WebActivationError("LAN runtime inverse consumer path is invalid")
            guards = _record_parent_guards(record, path)
            _path, capture = _capture_anchored(path, label=f"unchanged consumer {relative}", parent_guards=guards)
            if not _same_preimage_inode(capture, _receipt_preimage(record, tx)):
                return False
        _runtime_tls_publication(paths, _consumer_tls_publication(publication))
        return True

    captures: dict[str, tuple[
        Path,
        release_manager._AnchoredFileSnapshot,
        release_manager._PublicationParentGuards,
    ]] = {}
    for relative in GENERATED_RUNTIME:
        record = preimages.get("generated:" + relative)
        if not isinstance(record, Mapping):
            raise WebActivationError(f"LAN runtime inverse lacks preimage: {relative}")
        path = _record_target(record)
        if path != _publication_path(paths.runtime / relative, label=f"inverse target {relative}"):
            raise WebActivationError(f"LAN runtime inverse preimage target is invalid: {relative}")
        guards = _record_parent_guards(record, path)
        preimage = _receipt_preimage(record, tx)
        _path, capture = _capture_anchored(
            path, label=f"unchanged runtime target {relative}", parent_guards=guards,
        )
        if not isinstance(capture, release_manager._AnchoredFileSnapshot):
            raise WebActivationError("LAN runtime inverse capture is invalid")
        if not _same_preimage_inode(capture, preimage):
            return False
        captures[relative] = (path, capture, guards)
    if captures["build-receipt.json"][1].snapshot.exists:
        # The exact original signed receipt proves the otherwise untracked
        # TLS directory and its complete leaf manifest also remain A.
        _validate_runtime_preparer_receipt(paths, captures)
        return True
    if any(capture.snapshot.exists for _path, capture, _guards in captures.values()):
        return False

    tls_path = _publication_path(paths.runtime / RUNTIME_TLS_CURRENT_PATH, label="bootstrap TLS target")
    receipt_guards = captures["build-receipt.json"][2]
    try:
        parents = release_manager._open_publication_parents(
            (tls_path,), parent_guards=receipt_guards,
        )
        try:
            parent = parents.for_path(tls_path)
            try:
                os.stat(tls_path.name, dir_fd=parent.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                # Even an empty existing directory may be a producer effect.
                # Only authenticated discovery may classify that generation.
                return False
            for relative, (path, capture, guards) in captures.items():
                _assert_runtime_capture_current(
                    path, capture, guards, label=f"unchanged runtime target {relative}",
                )
            parents.assert_current(phase="after checking absent bootstrap TLS")
            try:
                os.stat(tls_path.name, dir_fd=parent.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return True
            return False
        finally:
            parents.close()
    except release_manager.ReleaseError as exc:
        raise WebActivationError(str(exc)) from exc
    except OSError as exc:
        raise WebActivationError("bootstrap TLS absence could not be verified") from exc


def _reprepared_runtime_observation(
    paths: ActivationPaths,
    preimages: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    producer_tls: Mapping[str, object],
    not_before_ns: int,
) -> dict[str, object]:
    """Authenticate a fresh preparation and fence its separate consumer copies.

    Both runtime-only rollback and service restart use this observation. A
    signed producer inode is never substituted for a copied consumer inode.
    """

    signed, _identity, publication = _broker_runtime_receipt(paths)
    if (
        {field: signed[field] for field in ("files", "https_port", "lan_ip")} != policy
        or signed["tls_publication"]["current"] != producer_tls
        or _integer(signed["producer_attestation"].get("issued_at_ns"), label="restart attestation time", minimum=1) < not_before_ns
    ):
        raise WebActivationError("rollback service preparation differs from its authorized A inputs")
    for relative in GENERATED_RUNTIME:
        record = preimages["generated:" + relative]
        path = _record_target(record)
        if path != _publication_path(paths.runtime / relative, label="reprepared consumer"):
            raise WebActivationError("rollback preparation consumer path changed")
        claim = publication["receipt"] if relative == "build-receipt.json" else publication["outputs"][relative]
        expected = _runtime_broker_postimage(record, path, claim, expected_mode=0o644 if relative == "build-receipt.json" else 0o600)
        guards = _record_parent_guards(record, path)
        _path, capture = _capture_anchored(path, label="rollback restarted consumer", parent_guards=guards)
        _expected_postimage(path, capture, expected)
    _runtime_tls_publication(paths, _consumer_tls_publication(publication))
    return {"receipt": signed, "publication": publication}


def _restore_runtime_with_unchanged_tls(
    paths: ActivationPaths,
    tx: Path,
    record: dict[str, object],
    preimages: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
) -> bool:
    """Republish A when A/B changed runtime generation but retained exact TLS.

    The historical TLS inverse intent, if any, remains evidence. Its TLS
    generation cannot identify this runtime transition. A separate durable
    intent permits normal preparation only from exact owned B consumer leaves;
    a retry may instead adopt a fresh authenticated A successor. Completed
    replay checks the checkpointed copies without invoking the broker.
    """

    pending = record.get("runtime_reprepare")
    if paths.runtime != service_wrappers.PREPARER_RUNTIME_ROOT:
        if pending is not None:
            raise WebActivationError("runtime-only rollback requires its canonical broker")
        return False
    if pending is None and (record.get("tls_publication") is None or record.get("runtime_rollback") is not None):
        return False
    old_capture = _receipt_preimage(record, tx)
    if not old_capture.snapshot.exists:
        if pending is not None:
            raise WebActivationError("runtime-only rollback lacks original A receipt")
        return False
    old = _strict_json_object(old_capture.snapshot.content, label="original A runtime receipt")
    if pending is None and old.get("tls_publication") != record.get("tls_publication"):
        return False
    if any(record.get(name) is not None for name in ("runtime_rollback", "tls_rollback", "tls_receipt_tombstoned")):
        raise WebActivationError("runtime-only rollback conflicts with a TLS inverse")

    def authenticate(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise WebActivationError("runtime-only rollback receipt is invalid")
        try:
            runtime_attestation.verify_runtime_build_attestation(value, config=_runtime_attestation_config(paths))
        except runtime_attestation.AttestationError as exc:
            raise WebActivationError("runtime-only rollback receipt attestation is invalid") from exc
        return dict(value)

    old = authenticate(old)
    tls = _parse_runtime_tls_publication(old["tls_publication"])
    historical_intent = record.get("runtime_rollback_intent")
    if historical_intent is not None and historical_intent != {
        "schema": "termdeck.web-activation-runtime-inverse.v1", "mode": "explicit", "generation": tls["generation"],
    }:
        raise WebActivationError("runtime-only rollback historical inverse intent is invalid")
    if record.get("tls_publication") != tls:
        raise WebActivationError("runtime-only rollback producer TLS changed")
    policy = {field: old[field] for field in ("files", "https_port", "lan_ip")}

    def generated() -> dict[str, object]:
        result = {}
        for relative in GENERATED_RUNTIME:
            item = preimages.get("generated:" + relative)
            if not isinstance(item, dict):
                raise WebActivationError("runtime-only rollback lacks generated preimages")
            path = _record_target(item)
            if path != _publication_path(paths.runtime / relative, label="runtime-only consumer"):
                raise WebActivationError("runtime-only rollback consumer path changed")
            guards = _record_parent_guards(item, path)
            _path, capture = _capture_anchored(path, label="runtime-only consumer", parent_guards=guards)
            result[relative] = _postimage_payload(path, capture, guards)
        return result

    def sources() -> dict[str, object]:
        result = {}
        for relative in sorted(name for name in RUNTIME_PREPARER_SOURCE_FILES if not name.startswith("tls/")):
            item = preimages.get("runtime:" + relative)
            if not isinstance(item, Mapping):
                raise WebActivationError("runtime-only rollback lacks restored A sources")
            path = _record_target(item)
            if path != _publication_path(paths.runtime / relative, label="runtime-only source"):
                raise WebActivationError("runtime-only rollback source path changed")
            guards = _record_parent_guards(item, path)
            _path, capture = _capture_anchored(path, label="runtime-only source", parent_guards=guards)
            original = _receipt_preimage(item, tx)
            if not _same_preimage_inode(capture, original):
                _expected_postimage(path, capture, item.get("rollback_postimage"))
            if (
                not original.snapshot.exists or capture.snapshot != original.snapshot
                or hashlib.sha256(capture.snapshot.content).hexdigest() != policy["files"][relative]
            ):
                raise WebActivationError("runtime-only rollback source differs from original A")
            result[relative] = _postimage_payload(path, capture, guards)
        return result

    baseline_generated = {}
    for relative in GENERATED_RUNTIME:
        item = preimages.get("generated:" + relative)
        if not isinstance(item, dict) or not isinstance(item.get("postimage"), Mapping):
            raise WebActivationError("runtime-only rollback lacks owned B postimages")
        baseline_generated[relative] = item["postimage"]
    if pending is None:
        if generated() != baseline_generated:
            raise WebActivationError("runtime-only rollback B consumer postimages changed")
        path = _record_target(record)
        _path, capture = _capture_anchored(path, label="retained B receipt", parent_guards=_record_parent_guards(record, path))
        _expected_postimage(path, capture, record.get("postimage"))
        failed = authenticate(_strict_json_object(capture.snapshot.content, label="retained B receipt"))
        pending = {
            "schema": "termdeck.web-activation-runtime-reprepare.v1", "state": "intent",
            "not_before_ns": time.time_ns(), "historical_inverse": historical_intent,
            "original_receipt_sha256": hashlib.sha256(old_capture.snapshot.content).hexdigest(),
            "failed_receipt": failed, "sources": sources(),
            "baseline": {"generated": baseline_generated, "consumer_tls": record.get("consumer_tls_publication")},
            "observed": None,
        }
    fields = {"schema", "state", "not_before_ns", "historical_inverse", "original_receipt_sha256", "failed_receipt", "sources", "baseline", "observed"}
    if (
        not isinstance(pending, dict) or set(pending) != fields
        or pending.get("schema") != "termdeck.web-activation-runtime-reprepare.v1"
        or pending.get("state") not in {"intent", "preparing", "verified"}
        or (pending["state"] != "verified") != (pending.get("observed") is None)
        or pending.get("original_receipt_sha256") != hashlib.sha256(old_capture.snapshot.content).hexdigest()
        or pending.get("historical_inverse") != historical_intent
        or pending.get("baseline") != {"generated": baseline_generated, "consumer_tls": record.get("consumer_tls_publication")}
    ):
        raise WebActivationError("runtime-only rollback intent is invalid")
    failed = authenticate(pending["failed_receipt"])
    failed_bytes = runtime_attestation._canonical_json(failed)
    if (
        not isinstance(record.get("postimage"), Mapping)
        or hashlib.sha256(failed_bytes).hexdigest() != record["postimage"].get("sha256")
        or len(failed_bytes) != record["postimage"].get("size")
        or failed["tls_publication"] != tls
        or failed["lan_ip"] != old["lan_ip"] or failed["https_port"] != old["https_port"]
        or failed["runtime_publication"]["generation"] == old["runtime_publication"]["generation"]
        or any(failed["files"][name] != old["files"][name] for name in RUNTIME_PREPARER_SOURCE_FILES if name.startswith("tls/"))
    ):
        raise WebActivationError("runtime-only rollback A/B transition is not qualified")
    baseline_tls = _parse_runtime_tls_publication(pending["baseline"]["consumer_tls"])
    if (
        baseline_tls["generation"] != failed["runtime_publication"]["generation"]
        or baseline_tls["prior"] is not None
        or set(baseline_tls["current"]["manifest"]) != set(tls["current"]["manifest"])
        or any(
            baseline_tls["current"]["manifest"][name]["sha256"] != claim["sha256"]
            or baseline_tls["current"]["manifest"][name]["mode"] != claim["mode"]
            or baseline_tls["current"]["manifest"][name]["identity"][4] != claim["identity"][4]
            for name, claim in tls["current"]["manifest"].items()
        )
    ):
        raise WebActivationError("runtime-only rollback B consumer TLS is not its signed content")
    for relative in RUNTIME_PREPARER_OUTPUTS:
        original = _receipt_preimage(preimages["generated:" + relative], tx)
        claim = failed["runtime_publication"]["outputs"][relative]
        expected = baseline_generated[relative]
        if (
            not original.snapshot.exists or not isinstance(expected, Mapping)
            or claim["sha256"] != expected.get("sha256") or claim["mode"] != expected.get("mode")
            or hashlib.sha256(original.snapshot.content).hexdigest() != claim["sha256"]
            or original.snapshot.mode != claim["mode"]
        ):
            raise WebActivationError("runtime-only rollback changed original A outputs")
    not_before_ns = _integer(pending["not_before_ns"], label="runtime-only rollback time", minimum=1)
    if sources() != pending["sources"]:
        raise WebActivationError("runtime-only rollback A source binding changed")
    if record.get("runtime_reprepare") is None:
        if generated() != baseline_generated:
            raise WebActivationError("runtime-only rollback B changed before intent")
        _runtime_tls_publication(paths, pending["baseline"]["consumer_tls"])
        record["runtime_reprepare"] = pending
        checkpoint("intent:rollback:runtime-reprepare")

    def verify_result(observed: object) -> dict[str, object]:
        if not isinstance(observed, Mapping) or set(observed) != {"receipt", "publication"}:
            raise WebActivationError("runtime-only rollback observation is invalid")
        signed = authenticate(observed["receipt"])
        if (
            any(signed[field] != old[field] for field in ("files", "https_port", "lan_ip", "ok", "schema", "tls_publication"))
            or signed["runtime_publication"]["generation"] in {old["runtime_publication"]["generation"], failed["runtime_publication"]["generation"]}
            or _integer(signed["producer_attestation"].get("issued_at_ns"), label="runtime-only attestation time", minimum=1) < not_before_ns
        ):
            raise WebActivationError("runtime-only rollback successor differs from authorized A")
        publication = observed["publication"]
        try:
            service_wrappers._validate_preparer_publication_result(publication, operation="verify")
        except service_wrappers.WrapperError as exc:
            raise WebActivationError("runtime-only rollback consumer observation is invalid") from exc
        if publication["generation"] != signed["runtime_publication"]["generation"]:
            raise WebActivationError("runtime-only rollback successor generation changed")
        result = generated()
        for relative in GENERATED_RUNTIME:
            item = preimages["generated:" + relative]
            path = _record_target(item)
            claim = publication["receipt"] if relative == "build-receipt.json" else publication["outputs"][relative]
            expected = _runtime_broker_postimage(item, path, claim, expected_mode=0o644 if relative == "build-receipt.json" else 0o600)
            digest = hashlib.sha256(runtime_attestation._canonical_json(signed)).hexdigest() if relative == "build-receipt.json" else preimages["generated:" + relative]["sha256"]
            if result[relative] != expected or result[relative]["sha256"] != digest:
                raise WebActivationError("runtime-only rollback successor consumer changed")
        consumer_tls = _consumer_tls_publication(publication)
        if _capture_restored_consumer_tls(paths, consumer_tls["generation"], tls["current"]) != consumer_tls:
            raise WebActivationError("runtime-only rollback successor TLS changed")
        if sources() != pending["sources"]:
            raise WebActivationError("runtime-only rollback A sources changed during preparation")
        return result

    if pending["state"] == "verified":
        restored = verify_result(pending["observed"])
        if any(preimages["generated:" + relative].get("rollback_postimage") != restored[relative] for relative in GENERATED_RUNTIME):
            raise WebActivationError("runtime-only rollback completed postimages changed")
        if record.get("consumer_tls_rollback") != _consumer_tls_publication(pending["observed"]["publication"]):
            raise WebActivationError("runtime-only rollback completed TLS changed")
        return True
    if generated() == baseline_generated:
        _runtime_tls_publication(paths, pending["baseline"]["consumer_tls"])
        if sources() != pending["sources"]:
            raise WebActivationError("runtime-only rollback A sources changed before preparation")
        if pending["state"] != "intent":
            # The producer may have completed while consumer publication
            # rolled back to B. Its unchanged consumer baseline cannot prove
            # that a submitted request never ran. Preserve this uncertainty.
            raise WebActivationError("runtime-only rollback preparation outcome is uncertain; refusing to repeat it")
        pending["state"] = "preparing"
        checkpoint("intent:rollback:runtime-reprepare-request")
        if generated() != baseline_generated or sources() != pending["sources"]:
            raise WebActivationError("runtime-only rollback inputs changed before submission")
        _runtime_tls_publication(paths, pending["baseline"]["consumer_tls"])
        try:
            service_wrappers._request_lan_preparation(restore_mode="none", runtime_root=paths.runtime, timeout=90.0)
        except service_wrappers.WrapperError as exc:
            raise WebActivationError("runtime-only rollback broker preparation failed") from exc
    observed = _reprepared_runtime_observation(
        paths, preimages, policy=policy, producer_tls=tls["current"], not_before_ns=not_before_ns,
    )
    restored = verify_result(observed)
    # Fence again after all signature, source and consumer checks, before the
    # single durable checkpoint. Interrupted publication is adopted on retry.
    if generated() != restored:
        raise WebActivationError("runtime-only rollback successor changed before checkpoint")
    for relative in GENERATED_RUNTIME:
        preimages["generated:" + relative]["rollback_postimage"] = restored[relative]
        preimages["generated:" + relative]["rollback_restore"] = "restored"
    record["consumer_tls_rollback"] = _consumer_tls_publication(observed["publication"])
    pending.update(state="verified", observed=observed)
    checkpoint("rollback:runtime-reprepare")
    verify_result(observed)
    return True


def _restore_tls_generation(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    preimages: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None],
) -> bool:
    """Return True only when the producer restored and authenticated all of A.

    The caller restores A's source runtime first, retaining B's preparer, and
    skips ordinary generated-file restoration after True.  False denotes no
    producer mutation or the older authenticated TLS-only protocol.  Missing
    provenance, no-prior bootstrap, and ambiguous outputs remain actionable.
    """

    key = "generated:build-receipt.json"
    record = preimages.get(key)
    if not isinstance(record, dict):
        return False
    publication = record.get("tls_publication")
    if publication is not None and not isinstance(publication, Mapping):
        raise WebActivationError("generated runtime TLS publication is invalid")
    normalized = _parse_runtime_tls_publication(publication) if publication is not None else None
    generation = normalized["generation"] if normalized is not None else None
    prior = normalized["prior"] if normalized is not None else None
    intent = record.get("runtime_rollback_intent")
    completed = record.get("runtime_rollback")
    if _restore_runtime_with_unchanged_tls(paths, tx, record, preimages, checkpoint=checkpoint):
        return True
    history = receipt.get("phase_history")
    preparation_may_have_run = (
        receipt.get("status") == "services-starting"
        or isinstance(history, list) and "services-starting" in history
        or "generated_runtime_capture_error" in receipt
    )
    if normalized is None and intent is None and completed is None and not preparation_may_have_run:
        return False
    if normalized is None and intent is None and completed is None:
        if _runtime_inverse_has_no_changes(paths, tx, preimages):
            return False
    if normalized is not None and prior is None:
        raise WebActivationError("LAN runtime TLS publication has no authenticated prior generation")
    expected_intent = {
        "schema": "termdeck.web-activation-runtime-inverse.v1",
        "mode": "explicit" if normalized is not None else "discover",
        "generation": generation,
    }
    if intent is not None and (not isinstance(intent, Mapping) or dict(intent) != expected_intent):
        raise WebActivationError("LAN runtime inverse intent is invalid")
    if completed is not None:
        if intent is None:
            raise WebActivationError("LAN runtime inverse result lacks its durable intent")
        response = _parse_runtime_inverse_result(completed, intent=expected_intent, publication=normalized)
        if response.get("action") != "restored-runtime-generation":
            raise WebActivationError("LAN runtime inverse completion is invalid")
        _record_restored_runtime_generation(paths, tx, preimages, response, checkpoint=checkpoint, replay=True)
        return True

    marker = record.get("tls_rollback")
    if marker is not None:
        if not isinstance(prior, Mapping):
            raise WebActivationError("generated runtime TLS rollback lacks its prior generation")
        if marker != {
            "generation": generation,
            "restored": {
                "path": RUNTIME_TLS_CURRENT_PATH,
                "identity": prior.get("identity"),
                "manifest": prior.get("manifest"),
            },
        }:
            raise WebActivationError("generated runtime TLS rollback marker is invalid")
        expected = {
            "generation": generation,
            "current": marker["restored"],
            "prior": None,
        }
        _recover_tombstoned_receipt_publication(
            record, tx,
            checkpoint=lambda: checkpoint("rollback:tombstoned-receipt-recovery"),
        )
        if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
            # A completed consumer publication has its own durable inodes.
            # Re-publishing on replay would discard those inodes and might
            # remove the legacy receipt already restored by the outer inverse.
            consumer_tls = record.get("consumer_tls_rollback")
            if not isinstance(consumer_tls, Mapping):
                raise WebActivationError("LAN runtime TLS replay lacks consumer provenance")
            current = _capture_restored_consumer_tls(paths, generation, marker["restored"])
            if current != consumer_tls:
                raise WebActivationError("LAN runtime TLS consumer changed after restoration")
            _verify_consumer_receipt_tombstone(record, replay=True)
            return False
        if _runtime_tls_publication(paths, expected)["current"] != marker["restored"]:
            raise WebActivationError("generated runtime TLS rollback changed after restoration")
        receipt_path = _publication_path(
            paths.runtime / "build-receipt.json", label="LAN runtime build receipt"
        )
        parent_guards = _record_parent_guards(record, receipt_path)
        _path, current = _capture_anchored(
            receipt_path,
            label="LAN runtime build receipt after TLS rollback",
            parent_guards=parent_guards,
        )
        if current.snapshot.exists:
            restored_postimage = record.get("rollback_postimage")
            if (
                not isinstance(restored_postimage, Mapping)
                or _parent_guards_from_payload(restored_postimage.get("parent_guard"), receipt_path)
                != parent_guards
            ):
                raise WebActivationError("LAN runtime TLS rollback left an unowned public receipt")
            _expected_postimage(receipt_path, current, restored_postimage)
        return False

    # Normally B TLS must still be exactly the signed current generation.
    # A crash after the preparer's inverse exchange but before our marker is
    # the one exception: its B receipt is already tombstoned, and the
    # preparer's idempotent inverse command below is the only authority that
    # can distinguish the authenticated restored A from a foreign directory.
    if intent is None and normalized is not None and paths.runtime != service_wrappers.PREPARER_RUNTIME_ROOT:
        _runtime_tls_publication(paths, normalized)

    if intent is None:
        record["runtime_rollback_intent"] = expected_intent
        checkpoint("intent:rollback:generated-runtime")
    response = _request_runtime_preparation(
        paths,
        restore_mode=("generation" if normalized is not None else "unreceipted"),
        generation=generation if normalized is not None else None,
    )
    if response.get("action") == "no-prior-tls-generation":
        raise WebActivationError("LAN runtime TLS publication has no authenticated prior generation")
    response = _parse_runtime_inverse_result(response, intent=expected_intent, publication=normalized)
    if response["action"] == "restored-runtime-generation":
        _record_restored_runtime_generation(paths, tx, preimages, response, checkpoint=checkpoint, replay=False)
        return True
    if not isinstance(prior, Mapping):
        raise WebActivationError("LAN runtime TLS inverse lacks an authenticated prior generation")
    restored = {
        "path": RUNTIME_TLS_CURRENT_PATH,
        "identity": prior.get("identity"),
        "manifest": prior.get("manifest"),
    }
    if paths.runtime == service_wrappers.PREPARER_RUNTIME_ROOT:
        if response.get("restored") != restored or response.get("receipt") != "absent":
            raise WebActivationError("LAN runtime TLS broker result does not match its prior generation")
        consumer_tls = _capture_restored_consumer_tls(paths, generation, restored)
        _verify_consumer_receipt_tombstone(record, replay=False)
        record["tls_rollback"] = {"generation": generation, "restored": restored}
        record["consumer_tls_rollback"] = consumer_tls
        record["tls_receipt_tombstoned"] = True
        checkpoint("rollback:generated-tls-publication")
        _runtime_tls_publication(paths, consumer_tls)
        _verify_consumer_receipt_tombstone(record, replay=False)
        return False
    # Validate the descriptor-observed restored directory against the signed
    # A manifest rather than trusting stdout.  `prior` is authenticated by B's
    # producer commitment and includes full leaf identity/ctime tuples.
    expected = {"generation": generation, "current": restored, "prior": None}
    if _runtime_tls_publication(paths, expected)["current"] != restored:
        raise WebActivationError("LAN runtime TLS rollback did not restore its authenticated prior")
    receipt_path = _publication_path(
        paths.runtime / "build-receipt.json", label="LAN runtime build receipt"
    )
    parent_guards = _record_parent_guards(record, receipt_path)
    _path, current = _capture_anchored(
        receipt_path,
        label="LAN runtime build receipt after TLS rollback",
        parent_guards=parent_guards,
    )
    if current.snapshot.exists:
        raise WebActivationError("LAN runtime TLS rollback left a public B receipt")
    record["tls_rollback"] = {"generation": generation, "restored": restored}
    record["tls_receipt_tombstoned"] = True
    checkpoint("rollback:generated-tls-publication")
    return False


def _recover_tombstoned_receipt_publication(
    record: dict[str, object],
    tx: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Recover the receipt-bound publication from TLS tombstone to saved A."""

    nested = record.get("tombstone_publication")
    if nested is None:
        return
    if record.get("tls_receipt_tombstoned") is not True or not isinstance(nested, dict):
        raise WebActivationError("generated receipt tombstone publication is invalid")
    path = _record_target(record)
    guards = _record_parent_guards(record, path)
    original = _receipt_preimage(record, tx)
    if (
        _record_target(nested) != path
        or _record_parent_guards(nested, path) != guards
        or _receipt_preimage(nested, tx).snapshot.exists
        or not original.snapshot.exists
        or not isinstance(nested.get("publication_intent"), dict)
        or "rollback_publication" in nested
        or "rollback_postimage" in nested
    ):
        raise WebActivationError("generated receipt tombstone publication is not bound to A")
    _publication_intent(nested, path, tx, original.snapshot.content, original.snapshot.mode)
    _recover_record_publication(nested, tx, checkpoint=checkpoint)
    _path, current = _capture_anchored(
        path, label="recovered tombstoned receipt", parent_guards=guards,
    )
    if current.snapshot.exists:
        published = nested.get("postimage")
        if not isinstance(published, Mapping):
            raise WebActivationError("recovered tombstoned receipt lacks its owned postimage")
        _expected_postimage(path, current, published)
        if current.snapshot != original.snapshot:
            raise WebActivationError("recovered tombstoned receipt differs from saved A")
        changed = record.get("rollback_postimage") != published
        record["rollback_postimage"] = dict(published)
    else:
        # The authenticated inner inverse returned to its absent preimage.
        # Persist that result before another no-replace publication of A.
        changed = nested.get("postimage") is not None or "rollback_postimage" in record
        nested["postimage"] = None
        record.pop("rollback_postimage", None)
    if changed and checkpoint is not None:
        checkpoint()


def _restore_tombstoned_generated_receipt(
    record: dict[str, object],
    tx: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Restore A's build receipt only after authenticated TLS receipt tombstone.

    The TLS inverse intentionally removes B's public receipt before it swaps
    the directory.  A normal `_restore` would reject that absence because it
    expects B's exact receipt inode.  This narrow path accepts absence only
    after `_restore_tls_generation` checkpointed the descriptor-verified
    inverse, then publishes the original A receipt through the same anchored
    no-replace journal used by every outer restore.
    """

    if record.get("tls_receipt_tombstoned") is not True:
        raise WebActivationError("generated runtime receipt was not TLS-tombstoned")
    _recover_tombstoned_receipt_publication(record, tx, checkpoint=checkpoint)
    path = _record_target(record)
    parent_guards = _record_parent_guards(record, path)
    preimage = _receipt_preimage(record, tx)
    _path, current = _capture_anchored(
        path,
        label="generated runtime receipt after TLS inverse",
        parent_guards=parent_guards,
    )
    if _same_preimage_inode(current, preimage):
        return "already-restored"
    if current.snapshot.exists:
        restored = record.get("rollback_postimage")
        if not isinstance(restored, Mapping):
            raise WebActivationError("generated runtime receipt changed after TLS inverse")
        _expected_postimage(path, current, restored)
        if current.snapshot != preimage.snapshot:
            raise WebActivationError("generated runtime restored receipt differs from saved A")
        return "already-restored"
    if not preimage.snapshot.exists:
        return "already-restored"

    nested = record.get("tombstone_publication")
    if nested is None:
        nested = _preimage(path, tx, "tombstoned-generated-receipt")
        if (
            nested["exists"] is not False
            or _record_parent_guards(nested, path) != parent_guards
        ):
            raise WebActivationError("generated receipt changed before tombstone publication")
        record["tombstone_publication"] = nested

    def checkpoint_restored() -> None:
        record["rollback_postimage"] = dict(nested["postimage"])
        if checkpoint is not None:
            checkpoint()

    _publish_preimaged(
        nested,
        tx,
        preimage.snapshot.content,
        preimage.snapshot.mode,
        label="generated runtime receipt rollback target",
        checkpoint=checkpoint_restored,
        intent_checkpoint=checkpoint,
    )
    return "restored"


def _systemd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["/usr/bin/systemctl", "--user", *args], timeout=90, check=check)


def _unit_info(unit: str, *, check: bool = True) -> dict[str, str]:
    if unit not in SERVICES:
        raise WebActivationError(f"unit outside activation allowlist: {unit}")
    result = _systemd(
        "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "LoadState",
        "--no-pager", check=check,
    )
    return {key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines()) if key}


def _unit_stopped_or_absent(unit: str) -> bool:
    """Treat a missing managed unit as an already-complete stop.

    A rollback can be retried after its outer inverse restored a first
    install's unit preimages to absence.  `systemctl stop` reports that unit
    as not found even though there is no B process left to stop.  Read the
    documented load state without accepting other stop failures.
    """

    info = _unit_info(unit, check=False)
    return (
        info.get("LoadState") == "not-found"
        or info.get("ActiveState") in {"inactive", "failed"}
    )


def _stop_web() -> None:
    for unit in reversed(SERVICES):
        # A retry after a first-install inverse may already have restored the
        # generated unit to absence.  Do not ask systemd to stop an absent
        # name: absence is the idempotent completed state for this boundary.
        if _unit_stopped_or_absent(unit):
            continue
        stopped = _systemd("stop", unit, check=False)
        # A timed-out ExecStop can leave the unit inactive/failed after
        # systemd kills its control process. That is not a clean stop even
        # though a state-only observation says there is no active process.
        if stopped.returncode:
            raise WebActivationError(f"TermDeck web unit could not stop: {unit}")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not all(
        _unit_stopped_or_absent(unit) for unit in SERVICES
    ):
        time.sleep(0.25)
    if not all(_unit_stopped_or_absent(unit) for unit in SERVICES):
        raise WebActivationError("TermDeck web units did not stop in the bounded timeout")


def _web_service_start_ready(
    paths: ActivationPaths,
    unit: str,
    values: Mapping[str, str],
    *,
    layout: service_wrappers.WrapperLayout,
    command: tuple[str, ...],
    managed: bool,
    release_id: str | None,
    observed: dict[str, tuple[int, int, str]],
) -> bool:
    """Wait only for the expected wrapper to become its expected runtime.

    Type=simple reports running while the LAN wrapper is still preparing its
    publication. Its recorded config is written after preparation and nginx
    -t; require that identity to have actually exec'd before broker inspection.
    """

    spec = service_wrappers.UNIT_SPEC_BY_NAME[unit]
    if (
        values.get("FragmentPath") != str(paths.unit_dir / unit)
        or values.get("DropInPaths") != ""
        or values.get("NeedDaemonReload") != "no"
        or values.get("LoadState") != "loaded"
        or values.get("Type") != "simple"
    ):
        raise WebActivationError(f"web startup unit binding changed: {unit}")
    if values.get("ActiveState") == "activating":
        if unit in observed:
            raise WebActivationError(f"web startup invocation changed: {unit}")
        return False
    if values.get("ActiveState") != "active" or values.get("SubState") != "running":
        raise WebActivationError(f"web startup unit stopped or failed: {unit}")
    raw_pid = values.get("MainPID", "")
    invocation = values.get("InvocationID", "")
    if not raw_pid.isascii() or not raw_pid.isdecimal() or int(raw_pid) <= 0 or not _lower_hex(invocation, length=32):
        raise WebActivationError(f"web startup process identity is invalid: {unit}")
    pid = int(raw_pid)
    current = service_wrappers.process_identity(pid)
    identity = (pid, int(current["start_ticks"]), invocation)
    if observed.setdefault(unit, identity) != identity:
        raise WebActivationError(f"web startup process identity changed: {unit}")
    expected = {
        "pid": pid, "start_ticks": identity[1],
        "exe": service_wrappers._expected_proc_executable(command[0]),
        "argv": list(service_wrappers._expected_proc_argv(command)),
    }
    if not service_wrappers.verify_process_identity(expected):
        if managed:
            wrapper = (str(layout.wrapper_path(spec, "start")),)
            preparing = {
                **expected,
                "exe": service_wrappers._expected_proc_executable(wrapper[0]),
                "argv": list(service_wrappers._expected_proc_argv(wrapper)),
            }
            if service_wrappers.verify_process_identity(preparing):
                return False
            # exec can occur between the two /proc observations above.
            if not service_wrappers.verify_process_identity(expected):
                raise WebActivationError(f"web startup has an unexpected process: {unit}")
        else:
            raise WebActivationError(f"web startup has an unexpected process: {unit}")
    if managed:
        config, snapshot = service_wrappers._read_unit_config_snapshot(
            layout.config_path(spec), spec=spec, expected_release_id=release_id,
        )
        if (
            config.get("pid") != pid or config.get("start_ticks") != identity[1]
            or config.get("state") not in {"starting", "running"}
        ):
            raise WebActivationError(f"web startup config does not own its process: {unit}")
        service_wrappers._validate_recorded_command(spec, layout, config)
        service_wrappers._assert_unit_config_snapshot(snapshot, spec=spec, expected_release_id=release_id)
    if spec.kind == "lan":
        pid_path = service_wrappers._runtime_paths(layout)["nginx_pid"]
        if not os.path.lexists(pid_path):
            return False
        if service_wrappers._read_nginx_pid(layout) != pid:
            raise WebActivationError("web startup LAN process does not own its pidfile")
    return service_wrappers.verify_process_identity(expected)


def _start_web(paths: ActivationPaths) -> None:
    deadline = time.monotonic() + 90

    def systemd(*args: str) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebActivationError("TermDeck web runtime did not become ready in 90 seconds")
        return _run(["/usr/bin/systemctl", "--user", *args], timeout=remaining)

    try:
        layout = service_wrappers.resolve_layout(**_wrapper_kwargs(paths))
        release_state = _managed_release_state_snapshot(paths)
        release_id = release_state["release_id"] if release_state is not None else None
        contracts = {}
        for unit in SERVICES:
            if unit not in {"termdeck-web-backend.service", "termdeck-web-lan.service"}:
                raise WebActivationError(f"unit outside activation allowlist: {unit}")
            spec = service_wrappers.UNIT_SPEC_BY_NAME[unit]
            guards = _capture_parent_guards((paths.unit_dir / unit,))
            path, capture = _capture_anchored(paths.unit_dir / unit, label="web startup unit", parent_guards=guards)
            if not capture.snapshot.exists:
                raise WebActivationError(f"web startup unit is absent: {unit}")
            properties: dict[str, list[str]] = {}
            section = ""
            for raw in capture.snapshot.content.decode("utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                elif section == "Service":
                    key, separator, value = line.partition("=")
                    if not separator or line.endswith("\\"):
                        raise WebActivationError("web startup unit syntax is unsupported")
                    properties.setdefault(key, []).append(value)
            if properties.get("Type") != ["simple"] or len(properties.get("ExecStart", [])) != 1:
                raise WebActivationError("web startup service type or command is unsupported")
            value = properties["ExecStart"][0]
            if "$" in value or "%" in value:
                raise WebActivationError("web startup command expansion is unsupported")
            start = tuple(shlex.split(value))
            command = service_wrappers.build_start_command(spec, **_wrapper_kwargs(paths))
            managed = start == (str(layout.wrapper_path(spec, "start")),)
            if managed:
                if not isinstance(release_id, str) or not release_id:
                    raise WebActivationError("web startup has no selected managed release")
                service_wrappers._read_unit_config_snapshot(layout.config_path(spec), spec=spec, expected_release_id=release_id)
            elif start != command or os.path.lexists(layout.config_path(spec)):
                raise WebActivationError("web startup direct command is not the original service contract")
            contracts[unit] = (path, capture, guards, command, managed)
        if len({contract[4] for contract in contracts.values()}) != 1:
            raise WebActivationError("web startup service modes are mixed")
        for unit in SERVICES:
            systemd("start", unit)
        observed: dict[str, tuple[int, int, str]] = {}
        while True:
            ready = True
            for unit, (path, capture, guards, command, managed) in contracts.items():
                _assert_runtime_capture_current(path, capture, guards, label="web startup unit")
                info = systemd("show", unit, "-p", "MainPID", "-p", "InvocationID", "-p", "ActiveState", "-p", "SubState", "-p", "LoadState", "-p", "Type", "-p", "FragmentPath", "-p", "DropInPaths", "-p", "NeedDaemonReload", "--no-pager")
                values = dict(line.partition("=")[::2] for line in info.stdout.splitlines() if "=" in line)
                ready = _web_service_start_ready(
                    paths, unit, values, layout=layout, command=command, managed=managed,
                    release_id=release_id, observed=observed,
                ) and ready
            if _managed_release_state_snapshot(paths) != release_state:
                raise WebActivationError("web startup managed release changed")
            if ready:
                for path, capture, guards, _command, _managed in contracts.values():
                    _assert_runtime_capture_current(path, capture, guards, label="web startup unit")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebActivationError("TermDeck web runtime did not become ready in 90 seconds")
            time.sleep(min(0.25, remaining))
    except (service_wrappers.WrapperError, UnicodeError, ValueError) as exc:
        raise WebActivationError(f"web startup readiness failed: {exc}") from exc


def _arm(
    paths: ActivationPaths,
    tx: Path,
    *,
    preimage: dict[str, object] | None = None,
    checkpoint: Callable[[str], None] | None = None,
    recovery_command: Sequence[str] | None = None,
) -> None:
    content = _watchdog_unit(paths, tx, recovery_command=recovery_command).encode()
    if preimage is None:
        _atomic(paths.unit_dir / WATCHDOG_UNIT, content, 0o644)
    else:
        _publish_preimaged(
            preimage,
            tx,
            content,
            0o644,
            label=f"activation unit {WATCHDOG_UNIT}",
            checkpoint=(
                (lambda: checkpoint("unit:" + WATCHDOG_UNIT))
                if checkpoint is not None
                else None
            ),
            intent_checkpoint=(
                (lambda: checkpoint("intent:unit:" + WATCHDOG_UNIT))
                if checkpoint is not None
                else None
            ),
        )
    _systemd("daemon-reload")
    _systemd("enable", "--now", WATCHDOG_UNIT)
    if _systemd("is-active", WATCHDOG_UNIT, check=False).returncode:
        raise WebActivationError("independent recovery watchdog did not arm")


def _disarm() -> None:
    # A recovery worker is the main process of WATCHDOG_UNIT.  Stopping its
    # own unit here would terminate it (ExecStop and KillMode=control-group)
    # before the package/runtime inverse or terminal receipt can complete.
    # Disable future starts instead; watchdog() exits normally only after the
    # inverse returns.  Other callers still require a fully inactive unit.
    recovering_self = getattr(_WATCHDOG_EXECUTION, "recovering", False)
    disable_args = ("disable", WATCHDOG_UNIT) if recovering_self else (
        "disable", "--now", WATCHDOG_UNIT,
    )
    disabled = _systemd(*disable_args, check=False)
    if disabled.returncode:
        raise WebActivationError("independent recovery watchdog did not disable")
    reloaded = _systemd("daemon-reload", check=False)
    if reloaded.returncode:
        raise WebActivationError("systemd daemon reload failed while disarming recovery watchdog")
    # Status queries use non-zero codes for both the expected stopped/disabled
    # states and errors.  Require the exact state as well as its exit code so
    # a failed query cannot authorize a durable disarm checkpoint.
    if not recovering_self:
        active = _systemd("is-active", WATCHDOG_UNIT, check=False)
        if not active.returncode:
            raise WebActivationError("independent recovery watchdog is still active after disarm")
        if active.returncode != 3 or active.stdout.strip() not in {"inactive", "failed"}:
            raise WebActivationError("could not verify recovery watchdog inactive state after disarm")
    enabled = _systemd("is-enabled", WATCHDOG_UNIT, check=False)
    if not enabled.returncode:
        raise WebActivationError("independent recovery watchdog is still enabled after disarm")
    if enabled.returncode != 1 or enabled.stdout.strip() != "disabled":
        raise WebActivationError("could not verify recovery watchdog disabled state after disarm")


def _health() -> dict[str, str]:
    value = _run(["/usr/bin/curl", "--silent", "--show-error", "--noproxy", "*", "--max-time", "8", "http://127.0.0.1:7391/_termdeck/healthz"], timeout=15).stdout.strip()
    if value != '{"ok":true,"service":"termdeck-web"}':
        raise WebActivationError("backend health contract failed")
    return {"backend": "ok"}


def _install(
    paths: ActivationPaths,
    package: Mapping[str, object],
    *,
    before_publication: Callable[[release_manager.PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[release_manager.PublicationTransactionReceipt], None] | None = None,
    migration: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result = install_release(
        str(package["wheel"]),
        str(package["manifest"]),
        root=paths.managed_root,
        bin_dir=paths.managed_bin,
        python=sys.executable,
        production=True,
        before_publication=before_publication,
        after_publication=after_publication,
        **({"migrate_from_root": migration["source_root"],
            "migrate_from_bin_dir": migration["source_bin_dir"]} if migration is not None else {}),
    )
    if result.get("ok") is not True:
        raise WebActivationError("qualified T1000 package install did not report success")
    verify_installed_tree(paths.managed_root / "releases" / str(result["release_id"]))
    return result


def _managed_release_state_snapshot(
    paths: ActivationPaths,
    *,
    require_generation: bool = False,
) -> dict[str, object] | None:
    """Read the selected managed release through a stable state-file FD.

    Watchdog arming records this exact state identity alongside the selected
    wrapper.  Ordinary rollback identity checks only need the release id, so
    older test receipts can still omit a generation until the point where an
    independently armed watchdog needs a durable generation binding.
    """

    state_path = paths.managed_root / "state.json"
    # ``exists`` silently treats a dangling state symlink as absent.  A
    # rollback must never mistake that foreign/tampered condition for the
    # authenticated empty legacy baseline.
    if not os.path.lexists(state_path):
        return None
    _path, capture = _capture_anchored(state_path, label="managed release state")
    if not capture.snapshot.exists:
        raise WebActivationError("managed release state disappeared while reading")
    try:
        state = json.loads(capture.snapshot.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WebActivationError("managed release state is not valid JSON") from exc
    if not isinstance(state, dict):
        raise WebActivationError("managed release state is not an object")
    current = state.get("current_release_id")
    if current is not None and not isinstance(current, str):
        raise WebActivationError("managed release state has an invalid current release id")
    generation = state.get("generation")
    if generation is None and not require_generation:
        return {
            "release_id": current,
            "generation": None,
            "sha256": hashlib.sha256(capture.snapshot.content).hexdigest(),
            "identity": _file_identity_payload(capture.identity),
        }
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise WebActivationError("managed release state has an invalid generation")
    if capture.identity is None:
        raise WebActivationError("managed release state has no stable identity")
    return {
        "release_id": current,
        "generation": generation,
        "sha256": hashlib.sha256(capture.snapshot.content).hexdigest(),
        "identity": _file_identity_payload(capture.identity),
    }


def _managed_release_id(paths: ActivationPaths) -> str | None:
    snapshot = _managed_release_state_snapshot(paths)
    if snapshot is None:
        return None
    current = snapshot["release_id"]
    if current is not None and not isinstance(current, str):  # defensive narrowing
        raise WebActivationError("managed release state has an invalid current release id")
    return current


def _watchdog_state_for_release(
    paths: ActivationPaths,
    expected_release_id: str,
) -> dict[str, object]:
    """Return the exact selected state that a recovery wrapper must use."""

    if not isinstance(expected_release_id, str) or not expected_release_id:
        raise WebActivationError("watchdog expected release id is invalid")
    snapshot = _managed_release_state_snapshot(paths, require_generation=True)
    if snapshot is None or snapshot.get("release_id") != expected_release_id:
        raise WebActivationError("managed release changed before recovery watchdog could arm")
    generation = snapshot.get("generation")
    digest = snapshot.get("sha256")
    identity = snapshot.get("identity")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(identity, Mapping)
    ):
        raise WebActivationError("managed release state cannot be bound to recovery watchdog")
    return snapshot


def _preflight_watchdog_wrapper(
    paths: ActivationPaths,
    expected_release_id: str,
) -> dict[str, object]:
    """Read back the wrapper set the independently started watchdog will run."""

    execution = _wrapper_execution_identity(paths, expected_release_id)
    try:
        verified = service_wrappers.preflight_wrappers(
            expected_release_id=expected_release_id,
            **_release_wrapper_kwargs(paths, expected_release_id),
        )
    except service_wrappers.WrapperError as exc:
        raise WebActivationError(f"recovery watchdog wrapper verification failed: {exc}") from exc
    if (
        verified.get("ok") is not True
        or verified.get("release_id") != expected_release_id
        or not isinstance(verified.get("manifest_sha256"), str)
        or len(str(verified.get("manifest_sha256"))) != 64
    ):
        raise WebActivationError("recovery watchdog wrapper verification returned an invalid identity")
    for key, value in execution.items():
        if verified.get(key) != value:
            raise WebActivationError(
                "recovery watchdog wrapper did not retain immutable execution binding"
            )
    return verified


def _watchdog_wrapper_postimage(
    paths: ActivationPaths,
    preimages: Mapping[str, object],
) -> dict[str, object]:
    """Capture the exact executable that systemd will use for recovery."""

    wrapper = _publication_path(
        paths.managed_root / "termdeck-web-recovery",
        label="recovery watchdog wrapper",
    )
    record = preimages.get("wrapper:" + str(wrapper))
    if not isinstance(record, Mapping) or _record_target(record) != wrapper:
        raise WebActivationError("recovery watchdog wrapper lacks a transaction preimage")
    parent_guards = _record_parent_guards(record, wrapper)
    _path, capture = _capture_anchored(
        wrapper,
        label="recovery watchdog wrapper",
        parent_guards=parent_guards,
    )
    if (
        not isinstance(capture, release_manager._AnchoredFileSnapshot)
        or not capture.snapshot.exists
        or capture.identity is None
    ):
        raise WebActivationError("recovery watchdog wrapper was not durably published")
    return _postimage_payload(wrapper, capture, parent_guards)


def _watchdog_manifest_postimage(
    paths: ActivationPaths,
    preimages: Mapping[str, object],
    expected_release_id: str,
    expected_manifest_sha256: str,
) -> dict[str, object]:
    """Capture the exact wrapper manifest paired with recovery's executable."""

    layout = service_wrappers.resolve_layout(
        **_release_wrapper_kwargs(paths, expected_release_id)
    )
    manifest = _publication_path(layout.manifest_path, label="recovery watchdog manifest")
    record = preimages.get("wrapper:" + str(manifest))
    if not isinstance(record, Mapping) or _record_target(record) != manifest:
        raise WebActivationError("recovery watchdog manifest lacks a transaction preimage")
    parent_guards = _record_parent_guards(record, manifest)
    _path, capture = _capture_anchored(
        manifest,
        label="recovery watchdog manifest",
        parent_guards=parent_guards,
    )
    if (
        not capture.snapshot.exists
        or capture.identity is None
        or capture.identity.nlink != 1
        or capture.snapshot.mode != service_wrappers.UNIT_CONFIG_MODE
    ):
        raise WebActivationError("recovery watchdog manifest was not durably published")
    value = _strict_json_object(capture.snapshot.content, label="recovery watchdog manifest")
    if (
        value.get("release_id") != expected_release_id
        or value.get("manifest_sha256") != expected_manifest_sha256
    ):
        raise WebActivationError("recovery watchdog manifest does not bind the selected release")
    return _postimage_payload(manifest, capture, parent_guards)


def _assert_current_postimage(
    path: Path,
    postimage: Mapping[str, object],
    *,
    label: str,
) -> None:
    """Re-read an exact leaf after a multi-file watchdog binding."""

    parent_guards = _parent_guards_from_payload(postimage.get("parent_guard"), path)
    _path, capture = _capture_anchored(path, label=label, parent_guards=parent_guards)
    _expected_postimage(path, capture, postimage)


def _watchdog_unit_postimage(
    paths: ActivationPaths,
    tx: Path,
    preimages: Mapping[str, object],
) -> dict[str, object]:
    """Validate the exact generated unit that was checkpointed before enable."""

    record = preimages.get("unit:" + WATCHDOG_UNIT)
    if not isinstance(record, Mapping):
        raise WebActivationError("watchdog publication lacks preimage")
    unit_path = _publication_path(paths.unit_dir / WATCHDOG_UNIT, label="recovery watchdog unit")
    if _record_target(record) != unit_path:
        raise WebActivationError("recovery watchdog unit preimage has the wrong target")
    parent_guards = _record_parent_guards(record, unit_path)
    _path, capture = _capture_anchored(
        unit_path,
        label="recovery watchdog unit",
        parent_guards=parent_guards,
    )
    postimage = record.get("postimage")
    _expected_postimage(unit_path, capture, postimage)
    if not isinstance(postimage, Mapping):  # narrowed by _expected_postimage above
        raise WebActivationError("recovery watchdog unit has no postimage")
    return dict(postimage)


def _arm_and_record_watchdog(
    paths: ActivationPaths,
    tx: Path,
    receipt: dict[str, object],
    preimages: Mapping[str, object],
    expected_release_id: str,
    *,
    arm_phase: str,
    checkpoint: Callable[[str], None],
) -> None:
    """Arm recovery only after binding its executable and selected release.

    For an A-to-B upgrade this runs while A remains selected, before any B
    package mutation.  For a first install it runs immediately after the
    release manager has durably published B and its wrapper journal, before
    runtime, unit, or service changes.  In either case the receipt contains a
    durable identity that lets a subsequent process distinguish a real armed
    watchdog from a crash before unit publication.
    """

    if arm_phase not in {"pre-package", "post-package"}:
        raise WebActivationError("recovery watchdog arm phase is invalid")
    if receipt.get("legacy_bootstrap") is not None or receipt.get("recovery_capsule") is not None:
        from . import legacy_activation
        legacy_activation.arm(paths, tx, receipt, preimages, checkpoint)
        return
    _watchdog_state_for_release(paths, expected_release_id)
    receipt["watchdog"] = {
        "unit": WATCHDOG_UNIT,
        "arming": True,
        "armed_before_disruption": False,
        "arm_phase": arm_phase,
        "expected_release_id": expected_release_id,
    }
    _persist(paths, tx, receipt, "watchdog-installing")
    unit_preimage = preimages.get("unit:" + WATCHDOG_UNIT)
    if not isinstance(unit_preimage, dict):
        raise WebActivationError("watchdog publication lacks mutable preimage")
    _arm(paths, tx, preimage=unit_preimage, checkpoint=checkpoint)

    # Bind state, the executable, and the manifest as one observed wrapper
    # generation after systemd has accepted the unit.  A second preflight and
    # exact leaf reread close the capture interval: a manifest from one
    # release and a recovery executable from another can never be persisted as
    # one watchdog identity.
    verified = _preflight_watchdog_wrapper(paths, expected_release_id)
    execution_identity = _wrapper_execution_identity(paths, expected_release_id)
    if any(verified.get(key) != value for key, value in execution_identity.items()):
        raise WebActivationError(
            "recovery watchdog execution binding changed while arming"
        )
    state_before_wrapper = _watchdog_state_for_release(paths, expected_release_id)
    wrapper = _watchdog_wrapper_postimage(paths, preimages)
    manifest = _watchdog_manifest_postimage(
        paths,
        preimages,
        expected_release_id,
        str(verified["manifest_sha256"]),
    )
    verified_after = _preflight_watchdog_wrapper(paths, expected_release_id)
    if verified_after["manifest_sha256"] != verified["manifest_sha256"]:
        raise WebActivationError("recovery watchdog manifest changed while binding its executable")
    if any(verified_after.get(key) != value for key, value in execution_identity.items()):
        raise WebActivationError(
            "recovery watchdog execution binding changed while arming"
        )
    _assert_current_postimage(
        _publication_path(
            paths.managed_root / "termdeck-web-recovery",
            label="recovery watchdog wrapper",
        ),
        wrapper,
        label="recovery watchdog wrapper",
    )
    _assert_current_postimage(
        service_wrappers.resolve_layout(**_wrapper_kwargs(paths)).manifest_path,
        manifest,
        label="recovery watchdog manifest",
    )
    state_after_wrapper = _watchdog_state_for_release(paths, expected_release_id)
    if state_after_wrapper != state_before_wrapper:
        raise WebActivationError("managed release changed while binding recovery watchdog")
    unit = _watchdog_unit_postimage(paths, tx, preimages)
    watchdog = receipt.get("watchdog")
    if not isinstance(watchdog, dict):
        raise WebActivationError("watchdog receipt state is invalid")
    watchdog.update(
        {
            "arming": False,
            "armed_before_disruption": True,
            "armed_identity": {
                "release_id": expected_release_id,
                "state_generation": state_after_wrapper["generation"],
                "state_sha256": state_after_wrapper["sha256"],
                "state_identity": state_after_wrapper["identity"],
                "wrapper": wrapper,
                "manifest": manifest,
                "wrapper_manifest_sha256": verified["manifest_sha256"],
                "unit": unit,
                **execution_identity,
            },
        }
    )
    _persist(paths, tx, receipt, "watchdog-armed")


def _rollback_package(
    paths: ActivationPaths,
    release: object,
    *,
    before_publication: Callable[[release_manager.PublicationTransactionReceipt], None] | None = None,
    after_publication: Callable[[release_manager.PublicationTransactionReceipt], None] | None = None,
) -> dict[str, object]:
    """Return package selection to the transaction's prior managed state.

    The release manager authenticates the currently selected release against
    the wrapper set before it changes either one.  Callers must therefore run
    this while the failed release's wrappers are still present.  A first
    deployment has no prior managed release; its authenticated inverse is the
    release manager's legacy rollback, not a rejected ``None`` release id.
    """

    kwargs: dict[str, object] = {
        "root": paths.managed_root,
        "bin_dir": paths.managed_bin,
        "production": True,
        "before_publication": before_publication,
        "after_publication": after_publication,
    }
    if release is None:
        kwargs["to_legacy"] = True
    elif isinstance(release, str):
        kwargs["release_id"] = release
    else:
        raise WebActivationError("prior managed release id is invalid")
    result = rollback_release(**kwargs)
    if result.get("ok") is not True:
        raise WebActivationError("qualified package rollback did not report success")
    return result


def _watchdog_may_be_armed(receipt: Mapping[str, object]) -> bool:
    watchdog = receipt.get("watchdog")
    if isinstance(watchdog, Mapping):
        # A completed rollback checkpoint is authoritative.  Retrying after a
        # crash beyond that point must not invent a second systemd action.
        if watchdog.get("disarmed_for_rollback") is True:
            return False
        # Persist the intent before crossing the systemd disable boundary.
        # A retry repeats the fixed, idempotent disarm when the process dies
        # before its completion checkpoint; skipping it here would leave the
        # still-armed watchdog able to terminate the recovery attempt.
        if watchdog.get("disarm_intent") is True:
            return True
        if watchdog.get("armed_before_disruption") is True:
            return True
        # ``arming`` is written before unit publication.  Do not try to
        # disable a nonexistent foreign unit when a process died in that gap;
        # the durable watchdog-unit postimage is the proof that enable could
        # have run.
        if watchdog.get("arming") is True:
            preimages = receipt.get("preimages")
            unit = (
                preimages.get("unit:" + WATCHDOG_UNIT)
                if isinstance(preimages, Mapping)
                else None
            )
            return isinstance(unit, Mapping) and isinstance(unit.get("postimage"), Mapping)
    history = receipt.get("phase_history")
    if not isinstance(history, list) or "watchdog-armed" not in history:
        return False
    return True


def _watchdog_transaction_is_latest(paths: ActivationPaths, tx: Path) -> bool:
    """Return whether this watchdog still owns the LATEST transaction.

    The generated recovery wrapper intentionally resolves ``--latest`` only
    when systemd starts it.  A later activation can therefore leave an older
    watchdog process running while LATEST already names T2.  That process may
    observe T1's stale heartbeat, but it has no authority to stop T2's units
    or disarm T2's recovery service.
    """

    try:
        return latest_transaction(paths).resolve() == tx.resolve()
    except (OSError, WebActivationError):
        # A missing or tampered LATEST pointer cannot safely authorize a
        # service mutation.  Let a separately started, authenticated
        # watchdog handle the next valid transaction instead.
        return False


def _watchdog_owns_published_successor(
    paths: ActivationPaths,
    tx: Path,
    receipt: Mapping[str, object],
    armed_release_id: str,
    armed: Mapping[str, object],
) -> bool:
    """Accept only this transaction's exact published A-to-B successor.

    The independently running watchdog imports retained A code.  Installing
    B intentionally replaces the public state, wrapper, and manifest which
    armed it.  The release-manager callback is the sole authority linking
    those original A leaves to their current B successors; phase history or
    a matching release id alone cannot establish that ownership.
    """

    watchdog = receipt.get("watchdog")
    package = receipt.get("package")
    if (
        not isinstance(watchdog, Mapping)
        or watchdog.get("arm_phase") != "pre-package"
        or receipt.get("old_release_id") != armed_release_id
        or not isinstance(package, Mapping)
    ):
        return False
    successor = package.get("release_id")
    if not isinstance(successor, str) or not successor or successor == armed_release_id:
        return False
    parsed = _release_publication_record_state(receipt, key="package_publication")
    if parsed is None:
        return False
    record, operation = parsed
    if (
        operation != "install"
        or record.get("state") != "published"
        or record.get("expected_release_id") != successor
    ):
        return False
    original = record.get("receipt")
    if not isinstance(original, Mapping):
        return False
    before = _release_publication_image_map(original, "preimages")
    # Inverse recovery may restore B using fresh authenticated inodes.  Its
    # exact callback chain, rather than the retired forward B identities,
    # supplies the effective postimages for this check.
    effective = _release_publication_recovered_handoff(record)
    after = _release_publication_image_map(effective, "postimages")
    parents = _release_publication_parent_guards_from_payload(effective.get("parents"))
    state_path = _publication_path(paths.managed_root / "state.json", label="managed release state")
    wrapper_path = _publication_path(paths.managed_root / "termdeck-web-recovery", label="recovery watchdog wrapper")
    manifest_path = service_wrappers.resolve_layout(**_wrapper_kwargs(paths)).manifest_path
    unit_path = _publication_path(paths.unit_dir / WATCHDOG_UNIT, label="recovery watchdog unit")
    state_before = before.get(state_path)
    generation = armed.get("state_generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or state_before is None
        or state_before.get("exists") is not True
        or state_before.get("identity") != armed.get("state_identity")
        or state_before.get("sha256") != armed.get("state_sha256")
        or not _outer_postimage_matches_release_image(wrapper_path, armed.get("wrapper"), before.get(wrapper_path))
        or not _outer_postimage_matches_release_image(manifest_path, armed.get("manifest"), before.get(manifest_path))
        or before[manifest_path].get("sha256") != armed.get("wrapper_manifest_sha256")
    ):
        return False
    unit = armed.get("unit")
    if not isinstance(unit, Mapping):
        return False

    def verify_current_images() -> None:
        _assert_current_postimage(unit_path, unit, label="recovery watchdog unit")
        for path in (state_path, wrapper_path, manifest_path):
            image = after.get(path)
            if image is None or image.get("exists") is not True or parents.for_path(path) is None:
                raise WebActivationError("watchdog successor lacks an exact publication image")
            _path, capture = _capture_anchored(
                path, label="recovery watchdog published successor", parent_guards=parents,
            )
            if _release_publication_snapshot_payload(path, capture) != image:
                raise WebActivationError("watchdog successor changed after publication")

    state = _watchdog_state_for_release(paths, successor)
    verify_current_images()
    verified = _preflight_watchdog_wrapper(paths, successor)
    if verified.get("manifest_sha256") != after[manifest_path].get("sha256"):
        return False
    if any(
        armed.get(key) != value
        for key, value in _wrapper_execution_identity(paths, armed_release_id).items()
    ):
        return False
    verify_current_images()
    return (
        _watchdog_state_for_release(paths, successor) == state
        and _watchdog_transaction_is_latest(paths, tx)
    )


def _watchdog_owns_current_armed_identity(
    paths: ActivationPaths,
    tx: Path,
    receipt: Mapping[str, object],
) -> bool:
    """Prove that a locked watchdog still owns the live recovery generation.

    The transaction lock serializes LATEST changes with activation, but it
    does not by itself prove that the process holding an old T1 receipt is the
    service instance that T2 armed.  Re-read every exact postimage persisted
    by ``_arm_and_record_watchdog`` while holding that lock.  Any mismatch is
    a benign stale-worker exit; this function deliberately performs no
    systemd action and never writes either receipt.
    """

    try:
        if not _watchdog_transaction_is_latest(paths, tx):
            return False
        if receipt.get("legacy_bootstrap") is not None or receipt.get("recovery_capsule") is not None:
            from . import legacy_activation
            return legacy_activation.owns(paths, tx, receipt)
        watchdog = receipt.get("watchdog")
        if not isinstance(watchdog, Mapping) or watchdog.get("unit") != WATCHDOG_UNIT:
            return False
        expected_release_id = watchdog.get("expected_release_id")
        armed = watchdog.get("armed_identity")
        expected_fields = {
            "release_id", "state_generation", "state_sha256", "state_identity",
            "wrapper", "manifest", "wrapper_manifest_sha256", "unit",
            "execution_root", "execution_binding", "control_root", "control_socket",
            "control_attestation",
        }
        if (
            not isinstance(expected_release_id, str)
            or not expected_release_id
            or not isinstance(armed, Mapping)
            or set(armed) != expected_fields
            or armed.get("release_id") != expected_release_id
        ):
            return False

        # The wrapper must still import the frozen selected release and retain
        # the root-attestor endpoint/schema that was armed.  Re-resolve this
        # from the release manager rather than trusting a mutable manifest.
        if any(
            armed.get(key) != value
            for key, value in _wrapper_execution_identity(
                paths, expected_release_id,
            ).items()
        ):
            return False

        current = _managed_release_state_snapshot(paths, require_generation=True)
        if current is not None and current.get("release_id") != expected_release_id:
            return _watchdog_owns_published_successor(
                paths, tx, receipt, expected_release_id, armed,
            )

        state = _watchdog_state_for_release(paths, expected_release_id)
        generation = armed.get("state_generation")
        state_digest = armed.get("state_sha256")
        state_identity = _file_identity_from_payload(armed.get("state_identity"))
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation != state.get("generation")
            or not isinstance(state_digest, str)
            or state_digest != state.get("sha256")
            or state_identity is None
            or armed.get("state_identity") != state.get("identity")
        ):
            return False

        wrapper = armed.get("wrapper")
        manifest = armed.get("manifest")
        unit = armed.get("unit")
        manifest_digest = armed.get("wrapper_manifest_sha256")
        if (
            not isinstance(wrapper, Mapping)
            or not isinstance(manifest, Mapping)
            or not isinstance(unit, Mapping)
            or not isinstance(manifest_digest, str)
            or len(manifest_digest) != 64
            or any(character not in "0123456789abcdef" for character in manifest_digest)
        ):
            return False

        wrapper_path = _publication_path(
            paths.managed_root / "termdeck-web-recovery",
            label="recovery watchdog wrapper",
        )
        manifest_path = service_wrappers.resolve_layout(
            **_wrapper_kwargs(paths)
        ).manifest_path
        unit_path = _publication_path(
            paths.unit_dir / WATCHDOG_UNIT,
            label="recovery watchdog unit",
        )
        _assert_current_postimage(
            wrapper_path, wrapper, label="recovery watchdog wrapper"
        )
        _assert_current_postimage(
            manifest_path, manifest, label="recovery watchdog manifest"
        )
        _assert_current_postimage(
            unit_path, unit, label="recovery watchdog unit"
        )
        verified = _preflight_watchdog_wrapper(paths, expected_release_id)
        if verified.get("manifest_sha256") != manifest_digest:
            return False
        if any(verified.get(key) != armed.get(key) for key in _wrapper_execution_identity(
            paths, expected_release_id,
        )):
            return False

        # Close the capture interval exactly as arming did: the wrapper and
        # manifest must still be the recorded leaves after wrapper preflight,
        # and state generation/identity must still identify this release.
        _assert_current_postimage(
            wrapper_path, wrapper, label="recovery watchdog wrapper"
        )
        _assert_current_postimage(
            manifest_path, manifest, label="recovery watchdog manifest"
        )
        _assert_current_postimage(
            unit_path, unit, label="recovery watchdog unit"
        )
        return _watchdog_state_for_release(paths, expected_release_id) == state
    except (OSError, WebActivationError, release_manager.ReleaseError):
        return False


def _b_services_may_be_live(receipt: Mapping[str, object]) -> bool:
    """Return whether a B web-service stop is required before package rollback."""

    marker = receipt.get("services_may_be_live")
    # Once an activation or an earlier rollback durably recorded its current
    # marker, it supersedes historical phases.  Retrying a first install after
    # its managed units were restored absent must not treat an old
    # ``services-starting`` entry as permission to stop unrelated/missing
    # units again.
    if marker is not None:
        if not isinstance(marker, bool):
            raise WebActivationError("services live marker is invalid")
        return marker
    # ``services-stopping`` is itself an asynchronous boundary: a crash can
    # land after B was selected but before `_stop_web()` first reached a unit.
    # Treat it just as conservatively as `services-starting`; package/wrapper
    # rollback must never race a possibly live B process.
    if receipt.get("status") in {"services-stopping", "services-starting"}:
        return True
    # Receipts written before the explicit marker used phase history as their
    # only durable evidence.  Both boundaries are persisted before systemd.
    history = receipt.get("phase_history")
    return isinstance(history, list) and any(
        phase in history for phase in ("services-stopping", "services-starting")
    )


def execute(paths: ActivationPaths, tx: Path) -> dict[str, object]:
    tx, receipt = load_transaction(paths, tx)
    if receipt.get("status") != "artifacts-staged":
        raise WebActivationError("execute requires an artifacts-staged transaction")
    if os.environ.get("TERMDECK_ACTIVATION_APPROVED") != "1":
        raise WebActivationError("live activation requires TERMDECK_ACTIVATION_APPROVED=1 after review")
    if paths.evidence != paths.database.parent / "recovery" / "activation-v2":
        raise WebActivationError("execute requires the recovery wrapper's canonical activation-v2 evidence root; prepare a new transaction there")
    from . import legacy_activation
    legacy_bootstrap = legacy_activation.prepare(paths, tx, receipt)
    if legacy_bootstrap is not None:
        receipt["legacy_bootstrap"] = legacy_bootstrap
    else:
        capsule = legacy_activation.prepare_recovery(paths, tx, receipt)
        if capsule is not None:
            receipt["recovery_capsule"] = capsule
    before = snapshot_database(paths, tx, "before")
    receipt["database_before"] = before
    receipt["processes_before"] = process_snapshot()
    preimages: dict[str, dict[str, object]] = {}
    receipt["preimages"] = preimages
    for relative in _sources(paths, tx):
        preimages["runtime:" + relative] = _preimage(
            paths.runtime / relative, tx, "runtime-" + relative.replace("/", "-"),
        )
    for relative in GENERATED_RUNTIME:
        preimages["generated:" + relative] = _preimage(
            paths.runtime / relative, tx, "generated-" + relative.replace("/", "-"),
        )
    for unit in (*SERVICES, WATCHDOG_UNIT):
        preimages["unit:" + unit] = _preimage(paths.unit_dir / unit, tx, "unit-" + unit)
    for path in _wrapper_publication_paths(paths):
        # Wrapper publication has a complete atomic transaction of its own;
        # retain these preimages in the activation receipt as the outer
        # rollback boundary for failures after publication.
        try:
            relative = path.relative_to(paths.managed_root)
            backup_name = "wrapper-managed-" + str(relative).replace("/", "-")
        except ValueError:
            try:
                relative = path.relative_to(paths.database.parent)
                backup_name = "wrapper-state-" + str(relative).replace("/", "-")
            except ValueError:
                backup_name = "wrapper-" + path.name
        preimages["wrapper:" + str(path)] = _preimage(path, tx, backup_name)
    receipt["old_release_id"] = _managed_release_id(paths)
    receipt["package_before"] = {"current_release_id": receipt["old_release_id"]}
    _persist(paths, tx, receipt, "mutation-start")

    def checkpoint(record: str) -> None:
        _checkpoint_publication(paths, tx, receipt, record)

    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(10):
            _persist(paths, tx, receipt)

    thread = threading.Thread(target=heartbeat, daemon=True)
    try:
        thread.start()
        package = receipt.get("package")
        if not isinstance(package, Mapping):
            raise WebActivationError("receipt lacks qualified package")
        release_id = package.get("release_id")
        if not isinstance(release_id, str) or not release_id:
            raise WebActivationError("qualified package receipt has no release id")
        old_release_id = receipt.get("old_release_id")
        if old_release_id is not None and (
            not isinstance(old_release_id, str) or not old_release_id
        ):
            raise WebActivationError("prior managed release id is invalid")

        # A rollback from B to A must retain an independently executable A
        # recovery path throughout package mutation.  It is deliberately
        # armed before calling the release manager, while A state and its
        # wrapper set can still be read back and bound in the receipt.
        if old_release_id is not None:
            _arm_and_record_watchdog(
                paths,
                tx,
                receipt,
                preimages,
                old_release_id,
                arm_phase="pre-package",
                checkpoint=checkpoint,
            )

        install_before, install_after = _release_publication_callbacks(
            paths,
            tx,
            receipt,
            preimages,
            key="package_publication",
            operation="install",
            expected_release_id=release_id,
            checkpoint=checkpoint,
        )
        _persist(paths, tx, receipt, "package-installing")
        receipt["package_result"] = _install(
            paths,
            package,
            before_publication=install_before,
            after_publication=install_after,
            **({"migration": legacy_bootstrap["migration"]} if legacy_bootstrap is not None else {}),
        )
        package_result = receipt.get("package_result")
        if not isinstance(package_result, Mapping):
            raise WebActivationError("qualified package install returned no result")
        _assert_release_publication_result(
            package_result,
            receipt,
            key="package_publication",
        )
        checkpoint("package:managed-release")
        _persist(paths, tx, receipt, "package-installed")
        if package_result.get("release_id") != release_id:
            raise WebActivationError("qualified package install selected an unexpected release")
        if _managed_release_id(paths) != release_id:
            raise WebActivationError("qualified package install did not durably select its release")
        # ``install_release`` publishes the release-bound wrapper set as part
        # of its own atomic managed-release transaction.  Keep a narrow
        # fallback for injected installers and older release-manager callers;
        # either path is completed before any web service stop.
        wrapper_result = package_result.get("service_wrappers")
        if not isinstance(wrapper_result, Mapping):
            wrapper_result = _publish_wrappers(paths, release_id)
        if (
            wrapper_result.get("ok") is not True
            or wrapper_result.get("release_id") != release_id
        ):
            raise WebActivationError("managed wrapper publication did not report success")
        receipt["wrapper_publication"] = dict(wrapper_result)
        receipt["wrappers_published"] = True
        receipt["wrapper_postimages"] = _capture_wrapper_postimages(
            paths,
            tx,
            preimages,
            release_id,
            checkpoint=checkpoint,
        )
        checkpoint("wrapper:managed-release")

        # There is no A executable on a first deployment.  The release
        # manager has now durably published B and its private journal has a
        # restart-recoverable terminal record; arm and verify B immediately,
        # before touching runtime files, web units, or web processes.
        if old_release_id is None:
            _arm_and_record_watchdog(
                paths,
                tx,
                receipt,
                preimages,
                release_id,
                arm_phase="post-package",
                checkpoint=checkpoint,
            )

        _persist(paths, tx, receipt, "runtime-publishing")
        for relative, (_source_path, mode) in _sources(paths, tx).items():
            runtime_preimage = preimages.get("runtime:" + relative)
            if runtime_preimage is None:  # defensive: receipt was just constructed above
                raise WebActivationError(f"runtime publication lacks preimage: {relative}")
            _copy(
                tx / "staged-runtime" / relative,
                paths.runtime / relative,
                mode,
                record=runtime_preimage,
                tx=tx,
                label=f"activation runtime {relative}",
                checkpoint=(lambda relative=relative: checkpoint("runtime:" + relative)),
                intent_checkpoint=(
                    lambda relative=relative: checkpoint("intent:runtime:" + relative)
                ),
            )
        _publish_units(paths, tx, preimages=preimages, checkpoint=checkpoint)
        receipt["service_units_published"] = True
        _systemd("daemon-reload")
        _persist(paths, tx, receipt, "runtime-staged")
        _persist(paths, tx, receipt, "services-stopping")
        _stop_web()
        receipt["services_stopped"] = True
        receipt["services_may_be_live"] = False
        _persist(paths, tx, receipt, "services-stopped")
        # Persist the possible-live marker before the start boundary.  A
        # timeout or process crash in _start_web can leave B running even when
        # the call never returns; rollback uses this marker to stop B before
        # changing the selected package or its wrappers.
        receipt["services_may_be_live"] = True
        _persist(paths, tx, receipt, "services-starting")
        _start_web(paths)
        _capture_generated_runtime_postimages(
            paths,
            tx,
            preimages,
            checkpoint=checkpoint,
        )
        receipt["health"] = _health()
        receipt["post_checks"] = {"database": verify_database(paths, tx, before), "processes": process_observation(receipt["processes_before"])}
        _disarm()
        watchdog = receipt.get("watchdog")
        if not isinstance(watchdog, dict):
            raise WebActivationError("watchdog receipt state is invalid")
        watchdog["arming"] = False
        watchdog["armed_after_activation"] = False
        watchdog["disarmed_after_activation"] = True
        _persist(paths, tx, receipt, "activated")
        return {"ok": True, "action": "activated", "transaction": str(tx), "receipt": receipt}
    except Exception as exc:
        if thread.is_alive():
            stop_heartbeat.set()
            thread.join()
        # The LAN wrapper invokes its descriptor-rooted preparation step while
        # systemd starts the service.  A start timeout can therefore arrive
        # after one or more generated leaves became visible but before the
        # normal post-start capture above ran.  Adopt every present exact
        # postimage now; absent leaves still equal their preimage and need no
        # rollback authority.  Existing captured postimages are revalidated,
        # never silently replaced with a later foreign value.
        phase_history = receipt.get("phase_history")
        if (
            receipt.get("status") == "services-starting"
            or isinstance(phase_history, list) and "services-starting" in phase_history
        ):
            try:
                _capture_generated_runtime_postimages(
                    paths,
                    tx,
                    preimages,
                    checkpoint=checkpoint,
                    allow_absent=True,
                )
            except Exception as capture_error:
                receipt["generated_runtime_capture_error"] = str(capture_error)
        receipt["error"] = str(exc)
        _persist(paths, tx, receipt, "failed")
        try:
            rollback(paths, tx, reason=str(exc))
        except Exception as rollback_error:
            # rollback() loads its own receipt and checkpoints every completed
            # inverse.  This execute-local object predates those checkpoints;
            # writing it again would erase restored inode identities and the
            # completed stop/disarm markers needed by the next retry.
            _tx, recovery_receipt = load_transaction(paths, tx)
            recovery_receipt["rollback_error"] = str(rollback_error)
            _persist(paths, tx, recovery_receipt, "rollback-failed")
            _atomic(paths.recovery_required, (str(tx) + "\n").encode())
        raise
    finally:
        stop_heartbeat.set()
        if thread.is_alive():
            thread.join()


def _clear_owned_recovery_marker(paths: ActivationPaths, tx: Path) -> None:
    """Remove only this transaction's unchanged recovery-required marker."""

    if not os.path.lexists(paths.recovery_required):
        return
    try:
        path = _publication_path(paths.recovery_required, label="activation recovery marker")
        guards = _capture_parent_guards((path,))
        _path, current = _capture_anchored(
            path, label="activation recovery marker", parent_guards=guards,
        )
        if not current.snapshot.exists or current.snapshot.content != (str(tx) + "\n").encode():
            return
        _publish_anchored(
            path, None, current.snapshot.mode,
            label="completed activation recovery marker",
            parent_guards=guards, expected_preimage=current,
        )
    except WebActivationError:
        # A foreign/changed marker is not ours to unlink and cannot invalidate
        # the already durable successful inverse of this transaction.
        return


def _rollback_restart_service_contract(
    paths: ActivationPaths, tx: Path, preimages: Mapping[str, object],
) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Select managed or original direct units from retained A unit bytes."""

    layout = service_wrappers.resolve_layout(**_wrapper_kwargs(paths))
    commands = {}
    modes = set()
    for unit in SERVICES:
        record = preimages.get("unit:" + unit)
        if not isinstance(record, Mapping):
            raise WebActivationError("rollback restart lacks its original unit")
        original = _receipt_preimage(record, tx)
        if not original.snapshot.exists:
            raise WebActivationError("rollback restart original unit is absent")
        properties: dict[str, list[str]] = {}
        section = ""
        try:
            for raw in original.snapshot.content.decode("utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                if section != "Service":
                    continue
                key, separator, value = line.partition("=")
                if not separator or line.endswith("\\"):
                    raise ValueError("unsupported unit syntax")
                properties.setdefault(key, []).append(value)
            if properties.get("Type") != ["simple"] or len(properties.get("ExecStart", [])) != 1:
                raise ValueError("unsupported service type or command")

            def argv(value: str) -> tuple[str, ...]:
                # These original units use literal arguments. Systemd
                # specifiers, variable expansion and command prefixes would
                # require a different authenticated execution contract.
                if "$" in value or "%" in value:
                    raise ValueError("expanded service command")
                return tuple(shlex.split(value))

            start = argv(properties["ExecStart"][0])
            spec = service_wrappers.UNIT_SPEC_BY_NAME[unit]
            command = service_wrappers.build_start_command(spec, **_wrapper_kwargs(paths))
            commands[unit] = tuple(command)
            if start == (str(layout.wrapper_path(spec, "start")),):
                modes.add("managed")
                continue
            if start != tuple(command):
                raise ValueError("unexpected direct service command")
            modes.add("legacy-direct")
            if spec.kind == "backend":
                pre = [("/usr/bin/test", "-x", str(paths.runtime / "bin/termdeck-web-server")),
                       ("/usr/bin/test", "-r", str(paths.database))]
                reload = []
            else:
                pre = [(str(paths.runtime / "bin/termdeck-web-prepare"),),
                       ("/usr/bin/nginx", "-t", "-p", str(paths.runtime / "nginx") + "/", "-c", str(paths.runtime / "nginx/nginx.conf"))]
                reload = [("/usr/bin/nginx", "-p", str(paths.runtime / "nginx") + "/", "-c", str(paths.runtime / "nginx/nginx.conf"), "-s", "reload")]
            if [argv(value) for value in properties.get("ExecStartPre", [])] != pre:
                raise ValueError("unexpected direct preparation command")
            if [argv(value) for value in properties.get("ExecReload", [])] != reload:
                raise ValueError("unexpected direct reload command")
            if any(key.startswith("Exec") and key not in {"ExecStart", "ExecStartPre", "ExecReload"} for key in properties):
                raise ValueError("unsupported direct service action")
        except (UnicodeError, ValueError) as exc:
            raise WebActivationError("rollback restart original service contract is unsupported") from exc
    if len(modes) != 1:
        raise WebActivationError("rollback restart original service modes are mixed")
    return modes.pop(), commands


def _rollback_service_restart(
    paths: ActivationPaths, tx: Path, receipt: dict[str, object],
) -> None:
    """Finish the canonical A-service restart without replaying A's inverse.

    A pending intent authorizes preparation for its exact restored inputs and
    TLS lineage. The broker has no invocation nonce: two valid preparations
    with those same bindings inside this window are observationally equivalent.
    Original direct units are a distinct legacy observation boundary: their
    unsigned files are observed only alongside the expected completed live
    startup and never become inverse/overwrite authority. Neither mode proves
    a unique writer within its pending window. Once observed, exact consumer
    and process identities replace that bounded permission. Legacy startup
    must preserve the restored TLS; incomplete startup after unsigned output
    changes requires review instead of another unowned write or inverse.
    """

    release_id = receipt.get("old_release_id")
    preimages = receipt.get("preimages")
    if (
        paths.runtime != service_wrappers.CANONICAL_RUNTIME_ROOT
        or not isinstance(release_id, str) or not release_id
        or receipt.get("services_stopped") is not True
        or not isinstance(preimages, Mapping)
    ):
        raise WebActivationError("rollback service restart lacks its canonical A boundary")
    try:
        mode, commands = _rollback_restart_service_contract(paths, tx, preimages)
        layout = service_wrappers.resolve_layout(**(
            _release_wrapper_kwargs(paths, release_id) if mode == "managed" else _wrapper_kwargs(paths)
        ))
        specs = {unit: service_wrappers.UNIT_SPEC_BY_NAME[unit] for unit in SERVICES}
        mutable_configs = {layout.config_path(spec) for spec in specs.values()}
        required = {"runtime:" + name for name in _sources(paths, tx)} | {
            "unit:" + unit for unit in SERVICES
        } | {"generated:" + name for name in GENERATED_RUNTIME} | {
            "wrapper:" + str(path) for path in _wrapper_publication_paths(paths)
        }
        if not required.issubset(preimages) or any(
            not isinstance(name, str) or not isinstance(record, Mapping)
            for name, record in preimages.items()
        ):
            raise WebActivationError("rollback service restart lacks complete A preimages")
        static_names = {
            name for name, record in preimages.items()
            if isinstance(name, str) and not name.startswith("generated:")
            and isinstance(record, Mapping)
            and (mode != "managed" or _record_target(record) not in mutable_configs)
        }

        def images(names: set[str], *, require_restored: bool = False) -> dict[str, object]:
            result: dict[str, object] = {}
            for name in sorted(names):
                record = preimages.get(name)
                if not isinstance(record, Mapping):
                    raise WebActivationError("rollback service restart preimage is invalid")
                path = _record_target(record)
                guards = _record_parent_guards(record, path)
                _path, capture = _capture_anchored(path, label="rollback restart input", parent_guards=guards)
                if require_restored and not _same_preimage_inode(capture, _receipt_preimage(record, tx)):
                    _expected_postimage(path, capture, record.get("rollback_postimage"))
                result[name] = _release_publication_snapshot_payload(path, capture)
            return result

        def binding() -> dict[str, object]:
            if mode == "managed":
                execution = _preflight_watchdog_wrapper(paths, release_id)
            else:
                # Legacy A has no managed service configs or wrapper execution
                # binding. Its installed package and direct executable leaves
                # are still required to remain the selected original inputs.
                verify_installed_tree(paths.managed_root / "releases" / release_id)
                executable_images = {}
                for command in commands.values():
                    executable = Path(service_wrappers._expected_proc_executable(command[0]))
                    _path, capture = _capture_anchored(executable, label="legacy restart executable")
                    executable_images[str(executable)] = _release_publication_snapshot_payload(executable, capture)
                execution = {"mode": mode, "commands": {key: list(value) for key, value in commands.items()}, "executables": executable_images}
            return {
                "release": _watchdog_state_for_release(paths, release_id),
                "wrappers": execution,
                "inputs": images(static_names),
            }

        def legacy_tls_observation() -> dict[str, object]:
            # A local observation, never a producer signature or authority to
            # restore these leaves. Exact identities are fenced by the common
            # descriptor-based TLS reader before use and after service start.
            root = paths.runtime / "tls"
            manifest = {}
            for name in sorted(os.listdir(root)):
                if name not in RUNTIME_TLS_ALLOWED_LEAVES:
                    raise WebActivationError("legacy restart TLS has an unexpected leaf")
                _path, capture = _capture_anchored(root / name, label="legacy restart TLS")
                if not capture.snapshot.exists or capture.identity is None:
                    raise WebActivationError("legacy restart TLS leaf is absent")
                manifest[name] = {
                    "mode": capture.snapshot.mode,
                    "identity": [getattr(capture.identity, field) for field in _FILE_IDENTITY_FIELDS],
                    "sha256": hashlib.sha256(capture.snapshot.content).hexdigest(),
                }
            info = root.lstat()
            observed = {"generation": "0" * 32, "prior": None, "current": {
                "path": "tls", "identity": list(_runtime_key_directory_identity(info, label="legacy restart TLS")), "manifest": manifest,
            }}
            return _runtime_tls_publication(paths, observed)

        generated_names = {"generated:" + name for name in GENERATED_RUNTIME}
        restart = receipt.get("rollback_service_restart")
        if restart is None:
            images(static_names, require_restored=True)
            generated = images(generated_names, require_restored=True)
            record = preimages["generated:build-receipt.json"]
            path = _record_target(record)
            guards = _record_parent_guards(record, path)
            _path, capture = _capture_anchored(path, label="restored A restart receipt", parent_guards=guards)
            base = _strict_json_object(capture.snapshot.content, label="restored A restart receipt")
            old = _strict_json_object(_receipt_preimage(record, tx).snapshot.content, label="original A restart receipt")
            for field in ("files", "https_port", "lan_ip"):
                if base.get(field) != old.get(field):
                    raise WebActivationError("rollback service restart changed original A inputs")
            if (
                base.get("ok") is not True or not isinstance(base.get("files"), Mapping)
                or set(base["files"]) != RUNTIME_PREPARER_SOURCE_FILES
                or any(not _lower_hex(value, length=64) for value in base["files"].values())
                or _integer(base.get("https_port"), label="restart HTTPS port", minimum=1) > 65535
            ):
                raise WebActivationError("rollback service restart original A policy is invalid")
            _runtime_private_ipv4(base.get("lan_ip"))
            if record.get("tls_receipt_tombstoned") is True:
                marker = record.get("tls_rollback")
                if not isinstance(marker, Mapping):
                    raise WebActivationError("rollback service restart lacks restored TLS authority")
                tls = marker.get("restored")
                generation = marker.get("generation")
            elif mode == "managed" or base.get("schema") == RUNTIME_PUBLICATION_SCHEMA:
                runtime_attestation.verify_runtime_build_attestation(base, config=_runtime_attestation_config(paths))
                parsed_tls = _parse_runtime_tls_publication(base.get("tls_publication"))
                tls, generation = parsed_tls["current"], parsed_tls["generation"]
            else:
                observed_tls = legacy_tls_observation()
                tls, generation = observed_tls["current"], observed_tls["generation"]
            if not isinstance(tls, Mapping) or not _lower_hex(generation, length=32):
                raise WebActivationError("rollback service restart TLS authority is invalid")
            consumer_tls = _capture_restored_consumer_tls(paths, generation, tls)
            restart = {
                "schema": "termdeck.web-activation-rollback-service-restart.v1",
                "state": "intent", "release_id": release_id, "mode": mode,
                "not_before_ns": time.time_ns(),
                "not_before_ticks": time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK") // 1_000_000_000,
                "binding": binding(),
                "baseline": {
                    "policy": {field: base[field] for field in ("files", "https_port", "lan_ip")},
                    "producer_tls": dict(tls) if mode == "managed" else None,
                    "generated": generated, "consumer_tls": consumer_tls,
                },
                "observed": None,
            }
            # Fence all leaves again before the durable permission to start.
            if images(generated_names) != generated or binding() != restart["binding"]:
                raise WebActivationError("rollback service restart inputs changed before intent")
            _runtime_tls_publication(paths, consumer_tls)
            receipt["rollback_service_restart"] = restart
            _checkpoint_publication(paths, tx, receipt, "intent:rollback-service-restart")
        fields = {"schema", "state", "release_id", "mode", "not_before_ns", "not_before_ticks", "binding", "baseline", "observed"}
        if (
            not isinstance(restart, dict) or set(restart) != fields
            or restart.get("schema") != "termdeck.web-activation-rollback-service-restart.v1"
            or restart.get("state") not in {"intent", "verified"}
            or restart.get("release_id") != release_id
            or restart.get("mode") != mode
            or not isinstance(restart.get("baseline"), Mapping)
            or set(restart["baseline"]) != {"policy", "producer_tls", "generated", "consumer_tls"}
            or (restart["state"] == "intent") != (restart.get("observed") is None)
        ):
            raise WebActivationError("rollback service restart intent is invalid")
        not_before_ns = _integer(restart["not_before_ns"], label="rollback restart time", minimum=1)
        not_before_ticks = _integer(restart["not_before_ticks"], label="rollback restart process time", minimum=1)
        baseline = restart["baseline"]
        if binding() != restart["binding"]:
            raise WebActivationError("rollback service restart A binding changed")

        def runtime_observation() -> dict[str, object]:
            if mode == "legacy-direct":
                _runtime_tls_publication(paths, baseline["consumer_tls"])
                generated = images(generated_names)
                for relative in GENERATED_RUNTIME:
                    record = preimages["generated:" + relative]
                    path = _record_target(record)
                    _path, capture = _capture_anchored(path, label="legacy restarted runtime", parent_guards=_record_parent_guards(record, path))
                    if capture.identity is None or capture.identity.mtime_ns < not_before_ns:
                        raise WebActivationError("legacy restart output predates its pending start")
                    if capture.snapshot.mode != (0o644 if relative == "build-receipt.json" else 0o600):
                        raise WebActivationError("legacy restart output mode changed")
                    if relative == "build-receipt.json":
                        parsed = _strict_json_object(capture.snapshot.content, label="legacy restarted receipt")
                        if parsed != {"schema": "termdeck.lan-runtime-build.v1", "ok": True, **baseline["policy"]}:
                            raise WebActivationError("legacy restart receipt differs from original A policy")
                    elif capture.snapshot.content != _receipt_preimage(record, tx).snapshot.content:
                        raise WebActivationError("legacy restart generated output differs from original A")
                for relative, digest in baseline["policy"]["files"].items():
                    if relative not in RUNTIME_PREPARER_SOURCE_FILES:
                        raise WebActivationError("legacy restart source claim is unsupported")
                    _path, capture = _capture_anchored(paths.runtime / relative, label="legacy restart source")
                    if hashlib.sha256(capture.snapshot.content).hexdigest() != digest:
                        raise WebActivationError("legacy restart source differs from its original receipt")
                return {"mode": "legacy-unsigned-observation", "generated": generated, "consumer_tls": baseline["consumer_tls"]}
            return _reprepared_runtime_observation(
                paths, preimages, policy=baseline["policy"],
                producer_tls=baseline["producer_tls"], not_before_ns=not_before_ns,
            )

        def services_observation() -> dict[str, object]:
            result = {}
            for unit, spec in specs.items():
                info = _systemd("show", unit, "-p", "MainPID", "-p", "InvocationID", "-p", "ActiveState", "-p", "SubState", "-p", "FragmentPath", "-p", "DropInPaths", "-p", "NeedDaemonReload", "--no-pager")
                values = dict(line.partition("=")[::2] for line in info.stdout.splitlines() if "=" in line)
                if (
                    values.get("FragmentPath") != str(paths.unit_dir / unit)
                    or values.get("DropInPaths") != "" or values.get("NeedDaemonReload") != "no"
                ):
                    raise WebActivationError("rollback restarted service is not its exact restored unit")
                if mode == "legacy-direct":
                    raw_pid = values.get("MainPID", "")
                    if not raw_pid.isascii() or not raw_pid.isdecimal():
                        raise WebActivationError("legacy restarted service PID is invalid")
                    pid = _integer(int(raw_pid), label="legacy service PID", minimum=1)
                    actual = service_wrappers.process_identity(pid)
                    expected = {**actual, "argv": list(service_wrappers._expected_proc_argv(commands[unit])),
                                "exe": service_wrappers._expected_proc_executable(commands[unit][0])}
                    if (
                        values.get("ActiveState") != "active" or values.get("SubState") != "running"
                        or not _lower_hex(values.get("InvocationID"), length=32)
                        or actual["start_ticks"] < not_before_ticks
                        or not service_wrappers.verify_process_identity(expected)
                    ):
                        raise WebActivationError("rollback restart has no expected live original A service")
                    if spec.kind == "lan" and service_wrappers._read_nginx_pid(layout) != pid:
                        raise WebActivationError("rollback original LAN process does not own its pidfile")
                    result[unit] = {"unit": values, "process": actual, "expected": expected}
                    continue
                config, snapshot = service_wrappers._read_unit_config_snapshot(layout.config_path(spec), spec=spec, expected_release_id=release_id)
                if (
                    values.get("ActiveState") != "active" or values.get("SubState") != "running"
                    or not _lower_hex(values.get("InvocationID"), length=32)
                    or str(config.get("pid")) != values.get("MainPID")
                    or _integer(config.get("start_ticks"), label="rollback service start ticks", minimum=1) < not_before_ticks
                    or config.get("state") not in {"starting", "running"}
                ):
                    raise WebActivationError("rollback restart has no expected live A service")
                service_wrappers._validate_recorded_command(spec, layout, config)
                if not service_wrappers.verify_process_identity(config):
                    raise WebActivationError("rollback restarted service process changed")
                if spec.kind == "lan" and service_wrappers._read_nginx_pid(layout) != config["pid"]:
                    raise WebActivationError("rollback restarted LAN process does not own its pidfile")
                service_wrappers._assert_unit_config_snapshot(snapshot, spec=spec, expected_release_id=release_id)
                result[unit] = {"unit": values, "config": config, "process": service_wrappers.process_identity(config["pid"]),
                                "postimage": _postimage_payload(snapshot.path, snapshot.preimage, snapshot.parent_guards)}
            return result

        if restart["state"] == "intent":
            # An interrupted start may have prepared a new generation already.
            # Before another idempotent start, require either the exact A
            # baseline or a fresh authenticated successor inside this intent.
            already_started = False
            if images(generated_names) == baseline["generated"]:
                _runtime_tls_publication(paths, baseline["consumer_tls"])
            else:
                if mode == "legacy-direct":
                    # Unsigned changed files do not authorize another start
                    # or inverse. Only an already completed expected original
                    # startup can be reconciled into a read-only observation.
                    services_observation()
                    already_started = True
                runtime_observation()
            if not already_started:
                _start_web(paths)
            observed = {"runtime": runtime_observation(), "services": services_observation()}
            if binding() != restart["binding"]:
                raise WebActivationError("rollback restart inputs changed during service start")
            if runtime_observation() != observed["runtime"] or services_observation() != observed["services"]:
                raise WebActivationError("rollback restart changed before its checkpoint")
            restart["observed"] = observed
            restart["state"] = "verified"
            _checkpoint_publication(paths, tx, receipt, "rollback-service-restart")
        observed = restart["observed"]
        if (
            not isinstance(observed, Mapping) or set(observed) != {"runtime", "services"}
            or runtime_observation() != observed["runtime"]
            or services_observation() != observed["services"]
            or binding() != restart["binding"]
        ):
            raise WebActivationError("rollback service restart changed after its checkpoint")
    except (service_wrappers.WrapperError, runtime_attestation.AttestationError) as exc:
        raise WebActivationError("rollback service restart authentication failed") from exc


def rollback(paths: ActivationPaths, tx: Path, *, reason: str) -> dict[str, object]:
    tx, receipt = load_transaction(paths, tx)
    if receipt.get("status") == "rolled-back":
        # A terminal inverse has already been durably observed.  Repeating a
        # rollback must not restart units, disarm a newer watchdog, or mutate
        # any receipt-owned file again.
        return {"ok": True, "action": "rolled-back", "transaction": str(tx), "receipt": receipt}
    if not receipt.get("mutation_started"):
        raise WebActivationError("rollback is not needed before the first mutation")
    watchdog_may_be_armed = _watchdog_may_be_armed(receipt)
    _persist(paths, tx, receipt, "rollback-start")
    try:
        old_release_id = receipt.get("old_release_id")
        if old_release_id is not None and (
            not isinstance(old_release_id, str) or not old_release_id
        ):
            raise WebActivationError("prior managed release id is invalid")
        preimages = receipt.get("preimages")
        if not isinstance(preimages, Mapping):
            raise WebActivationError("receipt lacks rollback preimages")

        if receipt.get("rollback_service_restart") is not None:
            # A's inverse is already complete. Its subsequent LAN startup is
            # allowed to create a new signed runtime generation; re-entering
            # the old inverse would mistake that authorized start for B.
            watchdog = receipt.get("watchdog")
            if not isinstance(watchdog, dict) or watchdog.get("disarmed_for_rollback") is not True:
                raise WebActivationError("rollback service restart lacks its disarmed watchdog")
            _rollback_service_restart(paths, tx, receipt)
            receipt["rollback_reason"] = reason
            receipt["post_checks"] = {"database": verify_database(paths, tx, receipt.get("database_before", {})), "processes": process_observation(receipt.get("processes_before", []))}
            watchdog["arming"] = False
            watchdog["armed_after_rollback"] = False
            _persist(paths, tx, receipt, "rolled-back")
            _clear_owned_recovery_marker(paths, tx)
            return {"ok": True, "action": "rolled-back", "transaction": str(tx), "receipt": receipt}

        # ``services-stopping`` and ``services-starting`` are asynchronous
        # boundaries.  A B process can outlive either state-pointer outcome:
        # an interrupted package journal may already have restored A while a
        # service that started B is still running.  Stop first, before either
        # journal recovery or an explicit B-to-A package inverse.
        if _b_services_may_be_live(receipt):
            _stop_web()
            receipt["services_stopped"] = True
            receipt["services_may_be_live"] = False
            _persist(paths, tx, receipt)

        # Disable the recovery unit while its selected wrapper still exists.
        # For a first install that wrapper is B, and package rollback removes
        # it; doing this after rollback would make ExecStop and systemd state
        # verification depend on a vanished executable.  The successful
        # checked disarm is checkpointed before package mutation so a retry is
        # idempotent if the process dies in the following boundary.
        watchdog = receipt.setdefault("watchdog", {"unit": WATCHDOG_UNIT})
        if not isinstance(watchdog, dict):
            raise WebActivationError("watchdog receipt state is invalid")
        if watchdog_may_be_armed:
            # The intent checkpoint is deliberately before the external
            # systemd action.  It closes the crash window in which the unit is
            # already disabled but the receipt still says it is armed.
            watchdog["disarm_intent"] = True
            _persist(paths, tx, receipt)
            _disarm()
            watchdog["arming"] = False
            watchdog["armed_before_disruption"] = False
            watchdog["disarmed_for_rollback"] = True
            watchdog["disarm_intent"] = False
            _persist(paths, tx, receipt)

        # Resolve every retained package journal while B's wrapper identity
        # is still intact but no B service can race the inverse.  This handles
        # a BaseException after the callback's durable outer checkpoint as
        # well as the earlier intent-only crash boundary.
        _recover_pending_release_publications(
            paths,
            tx,
            receipt,
            checkpoint=(
                lambda record: _checkpoint_publication(paths, tx, receipt, record)
            ),
        )
        current_release_id = _managed_release_id(paths)
        if receipt.get("legacy_bootstrap") is not None:
            from . import legacy_activation
            legacy_activation.restore_package(
                paths, tx, receipt,
                lambda record: _checkpoint_publication(paths, tx, receipt, record),
            )
            current_release_id = _managed_release_id(paths)
        receipt["package_rollback_required"] = current_release_id != old_release_id
        receipt["package_current_release_id"] = current_release_id
        _persist(paths, tx, receipt)

        # The release manager verifies the failed package's wrapper identity
        # before it will select the prior package.  The activation transaction
        # also owns an outer wrapper preimage for restoring live config bytes,
        # but applying it first turns B's authenticated wrapper set back into
        # A while state.json still selects B.  Roll package identity back
        # first, then restore all activation-owned external preimages in their
        # normal reverse transaction order.
        if current_release_id != old_release_id:
            _assert_package_rollback_owned(
                paths,
                receipt,
                preimages,
                current_release_id=current_release_id,
                old_release_id=old_release_id,
            )
            rollback_before, rollback_after = _release_publication_callbacks(
                paths,
                tx,
                receipt,
                preimages,
                key="package_rollback_publication",
                operation="rollback",
                expected_release_id=(
                    old_release_id if isinstance(old_release_id, str) else None
                ),
                checkpoint=(
                    lambda record: _checkpoint_publication(
                        paths, tx, receipt, record,
                    )
                ),
            )
            rollback_result = _rollback_package(
                paths,
                old_release_id,
                before_publication=rollback_before,
                after_publication=rollback_after,
            )
            _assert_release_publication_result(
                rollback_result,
                receipt,
                key="package_rollback_publication",
            )
            if _managed_release_id(paths) != old_release_id:
                raise WebActivationError("managed package identity did not return to its pre-activation release")
        # A process can die after the descriptor-rooted LAN preparer publishes
        # its final receipt but before execute captures outer postimages.  The
        # preparer receipt is the only authority that may adopt those leaves
        # during a retry; arbitrary regular files remain untouched/fail closed.
        generated_keys = {"generated:" + relative for relative in GENERATED_RUNTIME}
        present_generated = generated_keys.intersection(preimages)
        if present_generated and present_generated != generated_keys:
            raise WebActivationError("receipt has an incomplete generated runtime preimage set")
        generated_record = preimages.get("generated:build-receipt.json")
        inverse_pending = isinstance(generated_record, Mapping) and (
            generated_record.get("runtime_rollback_intent") is not None
            or generated_record.get("runtime_rollback") is not None
            or generated_record.get("runtime_reprepare") is not None
        )
        outer_generated_inverse_pending = any(
            isinstance(preimages.get(name), Mapping) and (
                "rollback_postimage" in preimages[name]
                or preimages[name].get("rollback_publication") is not None
            )
            for name in present_generated
        )
        if present_generated and not inverse_pending and not outer_generated_inverse_pending:
            try:
                _capture_generated_runtime_postimages(
                    paths,
                    tx,
                    preimages,
                    checkpoint=(
                        lambda record: _checkpoint_publication(
                            paths, tx, receipt, "rollback-adopt:" + record
                        )
                    ),
                    allow_absent=True,
                )
            except WebActivationError as exc:
                # A producer death may precede its final public receipt.  Only
                # its authenticated private inverse may resolve those leaves.
                receipt["generated_runtime_capture_error"] = str(exc)
                _persist(paths, tx, receipt)
        generated_names = generated_keys if present_generated else set()
        restored_sources: set[str] = set()
        if present_generated:
            # Reissuing A's attested receipt requires its original source
            # digests.  Keep B's authenticated preparer executable installed
            # until its directory/runtime inverse has completed.
            for name, record in reversed(list(preimages.items())):
                if not isinstance(name, str) or not name.startswith("runtime:"):
                    continue
                if name == "runtime:bin/termdeck-web-prepare":
                    continue
                if not isinstance(record, dict):
                    raise WebActivationError("receipt rollback runtime preimage is invalid")
                record["rollback_restore"] = _restore(
                    record,
                    tx,
                    checkpoint=(
                        lambda name=name: _checkpoint_publication(
                            paths, tx, receipt, "rollback:" + name,
                        )
                    ),
                )
                _checkpoint_publication(paths, tx, receipt, "rollback:" + name)
                restored_sources.add(name)
        runtime_restored = False
        if present_generated:
            runtime_restored = _restore_tls_generation(
                paths,
                tx,
                receipt,
                preimages,
                checkpoint=(
                    lambda record: _checkpoint_publication(paths, tx, receipt, record)
                ),
            ) is True
            if receipt.get("generated_runtime_capture_error") and not runtime_restored:
                raise WebActivationError(
                    "LAN runtime inverse did not resolve unreceipted preparation: "
                    + str(receipt["generated_runtime_capture_error"])
                )
        for name, record in reversed(list(preimages.items())):
            if name in generated_names or name in restored_sources:
                continue
            if not isinstance(record, dict):
                raise WebActivationError("receipt rollback preimage is invalid")
            restored = _restore(
                record,
                tx,
                checkpoint=(
                    lambda name=name: _checkpoint_publication(
                        paths, tx, receipt, "rollback:" + str(name)
                    )
                ),
            )
            record["rollback_restore"] = restored
            _checkpoint_publication(paths, tx, receipt, "rollback:" + str(name))
        if present_generated and not runtime_restored:
            # Keep the runtime receipt final: it is the producer's public
            # statement that map/current/TLS form one generation.  Releasing
            # it before map or current would advertise a mixed A/B set after a
            # crash.
            for relative in ("current-lan-ip", "nginx/allowed-hosts.map"):
                name = "generated:" + relative
                record = preimages.get(name)
                if not isinstance(record, dict):
                    raise WebActivationError("receipt rollback generated preimage is invalid")
                restored = _restore(
                    record,
                    tx,
                    checkpoint=(
                        lambda name=name: _checkpoint_publication(
                            paths, tx, receipt, "rollback:" + name
                        )
                    ),
                )
                record["rollback_restore"] = restored
                _checkpoint_publication(paths, tx, receipt, "rollback:" + name)
            receipt_name = "generated:build-receipt.json"
            receipt_record = preimages.get(receipt_name)
            if not isinstance(receipt_record, dict):
                raise WebActivationError("receipt rollback generated receipt preimage is invalid")
            if receipt_record.get("tls_receipt_tombstoned") is True:
                restored = _restore_tombstoned_generated_receipt(
                    receipt_record,
                    tx,
                    checkpoint=(
                        lambda: _checkpoint_publication(
                            paths, tx, receipt, "rollback:" + receipt_name
                        )
                    ),
                )
            else:
                restored = _restore(
                    receipt_record,
                    tx,
                    checkpoint=(
                        lambda: _checkpoint_publication(
                            paths, tx, receipt, "rollback:" + receipt_name
                        )
                    ),
                )
            receipt_record["rollback_restore"] = restored
            _checkpoint_publication(paths, tx, receipt, "rollback:" + receipt_name)
        _systemd("daemon-reload")
        # A first install has no prior service release to revive.  Likewise a
        # failure before a successful stop must leave the still-running old
        # services alone.  Only restart an authenticated old release after
        # this transaction durably recorded that it stopped those units.
        if old_release_id is not None and receipt.get("services_stopped") is True:
            if paths.runtime == service_wrappers.CANONICAL_RUNTIME_ROOT:
                # Persist completion of every inverse before a service can
                # prepare a successor. A retry resumes only this handoff.
                if watchdog.get("disarmed_for_rollback") is not True:
                    watchdog["disarmed_for_rollback"] = True
                    _persist(paths, tx, receipt)
                _rollback_service_restart(paths, tx, receipt)
            else:
                _start_web(paths)
        receipt["rollback_reason"] = reason
        receipt["post_checks"] = {"database": verify_database(paths, tx, receipt.get("database_before", {})), "processes": process_observation(receipt.get("processes_before", []))}
        watchdog["arming"] = False
        watchdog["armed_after_rollback"] = False
        _persist(paths, tx, receipt, "rolled-back")
        _clear_owned_recovery_marker(paths, tx)
        return {"ok": True, "action": "rolled-back", "transaction": str(tx), "receipt": receipt}
    except Exception:
        _persist(paths, tx, receipt, "rollback-failed")
        _atomic(paths.recovery_required, (str(tx) + "\n").encode())
        raise


def watchdog(paths: ActivationPaths, tx: Path) -> int:
    while True:
        _tx, receipt = load_transaction(paths, tx)
        status = str(receipt.get("status", ""))
        if status in TERMINAL or status == "activated":
            return 0
        # A systemd recovery worker resolves --latest only at process start.
        # If T2 has since replaced LATEST, T1 must stand down before it can
        # even consider its stale heartbeat.  The locked check below repeats
        # this under the activation lock before any rollback service action.
        if not _watchdog_transaction_is_latest(paths, _tx):
            return 0
        heartbeat = float(receipt.get("heartbeat_epoch", 0) or 0)
        if receipt.get("mutation_started") and heartbeat and time.time() - heartbeat > WATCHDOG_TIMEOUT:
            with _try_activation_lock(paths) as acquired:
                if acquired:
                    # Re-read after acquiring the lock so a live activation that
                    # refreshed its receipt cannot be mistaken for a stale owner.
                    # Before service mutation, also prove the exact state,
                    # wrapper, manifest, and unit generation which armed this
                    # worker still belongs to this transaction.  A stale T1
                    # returns cleanly and never stops or disarms T2.
                    locked_tx, locked_receipt = load_transaction(paths, tx)
                    if locked_receipt.get("status") in TERMINAL | {"activated"}:
                        return 0
                    if not _watchdog_owns_current_armed_identity(
                        paths, locked_tx, locked_receipt,
                    ):
                        return 0
                    locked_heartbeat = float(locked_receipt.get("heartbeat_epoch", 0) or 0)
                    if (
                        locked_receipt.get("mutation_started")
                        and locked_heartbeat
                        and time.time() - locked_heartbeat > WATCHDOG_TIMEOUT
                    ):
                        with _watchdog_recovery_context():
                            rollback(paths, locked_tx, reason="watchdog heartbeat expired")
                        return 0
                else:
                    time.sleep(2)
                    continue
        time.sleep(2)


def _paths(args: argparse.Namespace) -> ActivationPaths:
    names = ("target", "evidence", "runtime", "database", "managed_root", "managed_bin", "unit_dir")
    return ActivationPaths.defaults(**{name: Path(getattr(args, name)).resolve() for name in names if getattr(args, name)})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--execute", action="store_true")
    actions.add_argument("--rollback", action="store_true")
    actions.add_argument("--watchdog", action="store_true")
    parser.add_argument("--transaction", type=Path)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--reuse-web-binary", type=Path)
    parser.add_argument("--reuse-web-source-commit")
    parser.add_argument("--migrate-from-root", type=Path)
    parser.add_argument("--migrate-from-bin-dir", type=Path)
    for name in ("target", "evidence", "runtime", "database", "managed-root", "managed-bin", "unit-dir"):
        parser.add_argument("--" + name)
    args = parser.parse_args(argv)
    try:
        if not args.prepare and (args.migrate_from_root is not None or args.migrate_from_bin_dir is not None):
            raise WebActivationError("migration paths belong to --prepare; execute and rollback use that bound request")
        paths = _paths(args)
        if args.watchdog:
            tx = latest_transaction(paths) if args.latest else args.transaction
            if not tx:
                raise WebActivationError("watchdog requires --transaction or --latest")
            result = watchdog(paths, tx)
        else:
            with _activation_lock(paths):
                if args.prepare:
                    result = prepare(
                        paths,
                        reuse_web_binary=args.reuse_web_binary,
                        reuse_web_source_commit=args.reuse_web_source_commit,
                        migrate_from_root=args.migrate_from_root,
                        migrate_from_bin_dir=args.migrate_from_bin_dir,
                    )
                elif args.execute:
                    if not args.transaction:
                        raise WebActivationError("--execute requires --transaction")
                    result = execute(paths, args.transaction)
                else:
                    tx = latest_transaction(paths) if args.latest else args.transaction
                    if not tx:
                        raise WebActivationError("rollback requires --transaction or --latest")
                    result = rollback(paths, tx, reason="explicit recovery request")
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except (WebActivationError, release_manager.ReleaseError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
