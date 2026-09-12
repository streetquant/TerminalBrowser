"""Bounded, source-owned TermDeck web activation transaction."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping, Sequence

from .release_manager import build_release, install_release, rollback_release, verify_installed_tree

SCHEMA = "termdeck.web-activation.v2"
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
            os.environ.get("TERMDECK_STATE_ROOT", "/warp/state/termdeck")
        ).expanduser()
        if not state_root.is_absolute():
            raise WebActivationError("TERMDECK_STATE_ROOT must be an absolute path")
        database = Path(
            overrides.get("database") or state_root / "sessions.sqlite3"
        ).expanduser()
        if not database.is_absolute():
            raise WebActivationError("activation database must be an absolute path")
        return cls(
            source,
            Path(overrides.get("evidence") or "/warp/recovery/termdeck-migration/activation-v2").resolve(),
            Path(overrides.get("runtime") or "/warp/runtime/termdeck/termdeck-lan").resolve(),
            database.resolve(),
            Path(overrides.get("managed_root") or "/warp/runtime/termdeck/t1000-managed").resolve(),
            Path(overrides.get("managed_bin") or "/warp/runtime/termdeck/bin").resolve(),
            Path(overrides.get("unit_dir") or "/home/arch/.config/systemd/user").resolve(),
        )

    @property
    def latest(self) -> Path:
        return self.evidence / "LATEST"

    @property
    def recovery_required(self) -> Path:
        return self.evidence / "RECOVERY_REQUIRED"


_LOCK = threading.RLock()


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


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WebActivationError(f"unsafe activation destination: {path}")
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()


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
        raise WebActivationError(f"command could not complete: {argv[0]}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise WebActivationError(f"command failed: {Path(argv[0]).name}: {detail[-1] if detail else result.returncode}")
    return result


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "exists": False}
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise WebActivationError(f"activation path is not a regular file: {path}")
    return {"path": str(path), "exists": True, "sha256": _sha(path),
            "size": path.stat().st_size, "mode": stat.S_IMODE(mode)}


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
    if status in {"prepared", "artifacts-staged"}:
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


def _copy(source: Path, destination: Path, mode: int) -> None:
    if source.is_symlink() or not source.is_file():
        raise WebActivationError(f"staged source is not a regular file: {source}")
    _atomic(destination, source.read_bytes(), mode)


def _watchdog_unit(paths: ActivationPaths, tx: Path) -> str:
    wrapper = paths.target / "scripts/termdeck-web-activate.py"
    command = shlex.join(
        [
            "/usr/bin/python3",
            "-I",
            "-B",
            str(wrapper),
            "--watchdog",
            "--transaction",
            str(tx),
            "--target",
            str(paths.target),
            "--evidence",
            str(paths.evidence),
            "--runtime",
            str(paths.runtime),
            "--database",
            str(paths.database),
            "--managed-root",
            str(paths.managed_root),
            "--managed-bin",
            str(paths.managed_bin),
            "--unit-dir",
            str(paths.unit_dir),
        ]
    )
    writable = " ".join(
        str(path)
        for path in (
            tx,
            paths.evidence,
            paths.runtime,
            paths.unit_dir,
            paths.managed_root,
            paths.managed_bin,
        )
    )
    required_mounts = " ".join(
        str(path)
        for path in (
            Path("/warp"),
            paths.target,
            paths.evidence,
            paths.runtime,
            paths.database.parent,
            paths.managed_root,
            paths.managed_bin,
        )
    )
    return f"""[Unit]
Description=TermDeck web activation recovery watchdog
After=warp.mount default.target
ConditionPathIsMountPoint=/warp
RequiresMountsFor={required_mounts}

[Service]
Type=simple
ExecStart={command}
Restart=no
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={writable}
TimeoutStopSec=20s

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


def _publish_units(paths: ActivationPaths, tx: Path) -> None:
    for unit in SERVICES:
        staged = tx / "staged-units" / unit
        mode = _fingerprint(staged).get("mode", 0o644)
        _copy(staged, paths.unit_dir / unit, int(mode))


def prepare(
    paths: ActivationPaths,
    *,
    reuse_web_binary: Path | None = None,
    reuse_web_source_commit: str | None = None,
) -> dict[str, object]:
    source = _source(paths)
    tx, receipt = new_transaction(paths, source)
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
        else:
            environment = dict(os.environ)
            environment["CARGO_TARGET_DIR"] = str(cargo_target)
            _run(["/usr/bin/cargo", "build", "--release", "--locked", "-p", "termdeck-web-server"],
                 cwd=paths.target / "web", env=environment, timeout=1800)
            binary = cargo_target / "release/termdeck-web-server"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise WebActivationError("staged web build did not produce an executable")
            _run([str(binary), "--help"], cwd=paths.target / "web", timeout=30)
            receipt["web_binary"] = {
                "reused": False,
                "source_commit": source["commit"],
                "source_tree": current_web_tree,
                "current_source_tree": current_web_tree,
                "sha256": _sha(binary),
            }
        _copy(binary, artifacts / "web/termdeck-web-server", 0o755)
        staged_runtime, staged_units = tx / "staged-runtime", tx / "staged-units"
        for relative, (source_path, mode) in _sources(paths, tx).items():
            if OBSOLETE_RUNTIME_MARKER in source_path.read_bytes():
                raise WebActivationError(f"obsolete runtime marker in {relative}")
            _copy(source_path, staged_runtime / relative, mode)
        for unit in SERVICES:
            source_path = paths.target / "deploy/systemd" / unit
            if OBSOLETE_RUNTIME_MARKER in source_path.read_bytes():
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


def _preimage(path: Path, tx: Path, name: str) -> dict[str, object]:
    record = _fingerprint(path)
    backup = tx / "preimages" / name
    if record.get("exists"):
        _atomic(backup, path.read_bytes(), int(record["mode"]))
        record["backup"] = str(backup.relative_to(tx))
    else:
        record["backup"] = None
    return record


def _restore(record: Mapping[str, object], tx: Path) -> None:
    path, backup = Path(str(record["path"])), record.get("backup")
    if backup:
        _copy(tx / str(backup), path, int(record.get("mode", 0o600)))
    elif path.exists():
        if path.is_symlink() or not path.is_file():
            raise WebActivationError(f"unsafe rollback target: {path}")
        path.unlink()


def _systemd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["/usr/bin/systemctl", "--user", *args], timeout=90, check=check)


def _unit_info(unit: str) -> dict[str, str]:
    if unit not in SERVICES:
        raise WebActivationError(f"unit outside activation allowlist: {unit}")
    result = _systemd("show", unit, "-p", "ActiveState", "-p", "SubState", "--no-pager")
    return {key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines()) if key}


def _stop_web() -> None:
    for unit in reversed(SERVICES):
        _systemd("stop", unit)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not all(_unit_info(u).get("ActiveState") in {"inactive", "failed"} for u in SERVICES):
        time.sleep(0.25)
    if not all(_unit_info(u).get("ActiveState") in {"inactive", "failed"} for u in SERVICES):
        raise WebActivationError("TermDeck web units did not stop in the bounded timeout")


def _start_web() -> None:
    for unit in SERVICES:
        _systemd("start", unit)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not all(_unit_info(u).get("ActiveState") == "active" and _unit_info(u).get("SubState") == "running" for u in SERVICES):
        time.sleep(0.25)
    if not all(_unit_info(u).get("ActiveState") == "active" and _unit_info(u).get("SubState") == "running" for u in SERVICES):
        raise WebActivationError("TermDeck web units did not become active and running")


def _arm(paths: ActivationPaths, tx: Path) -> None:
    _atomic(paths.unit_dir / WATCHDOG_UNIT, _watchdog_unit(paths, tx).encode(), 0o644)
    _systemd("daemon-reload")
    _systemd("enable", "--now", WATCHDOG_UNIT)
    if _systemd("is-active", WATCHDOG_UNIT, check=False).returncode:
        raise WebActivationError("independent recovery watchdog did not arm")


def _disarm() -> None:
    _systemd("disable", "--now", WATCHDOG_UNIT, check=False)
    _systemd("daemon-reload", check=False)


def _health() -> dict[str, str]:
    value = _run(["/usr/bin/curl", "--silent", "--show-error", "--noproxy", "*", "--max-time", "8", "http://127.0.0.1:7391/_termdeck/healthz"], timeout=15).stdout.strip()
    if value != '{"ok":true,"service":"termdeck-web"}':
        raise WebActivationError("backend health contract failed")
    return {"backend": "ok"}


def _install(paths: ActivationPaths, package: Mapping[str, object]) -> dict[str, object]:
    result = install_release(str(package["wheel"]), str(package["manifest"]), root=paths.managed_root, bin_dir=paths.managed_bin, python=sys.executable, production=True)
    if result.get("ok") is not True:
        raise WebActivationError("qualified T1000 package install did not report success")
    verify_installed_tree(paths.managed_root / "releases" / str(result["release_id"]))
    return result


def _managed_release_id(paths: ActivationPaths) -> str | None:
    state_path = paths.managed_root / "state.json"
    if not state_path.exists():
        return None
    if state_path.is_symlink() or not state_path.is_file():
        raise WebActivationError("managed release state is not a regular file")
    state = _read(state_path)
    current = state.get("current_release_id")
    if current is not None and not isinstance(current, str):
        raise WebActivationError("managed release state has an invalid current release id")
    return current


def _rollback_package(paths: ActivationPaths, release: object) -> None:
    if not isinstance(release, str):
        raise WebActivationError("prior managed release id is unavailable")
    result = rollback_release(root=paths.managed_root, bin_dir=paths.managed_bin, release_id=release, production=True)
    if result.get("ok") is not True:
        raise WebActivationError("qualified package rollback did not report success")


def execute(paths: ActivationPaths, tx: Path) -> dict[str, object]:
    tx, receipt = load_transaction(paths, tx)
    if receipt.get("status") != "artifacts-staged":
        raise WebActivationError("execute requires an artifacts-staged transaction")
    if os.environ.get("TERMDECK_ACTIVATION_APPROVED") != "1":
        raise WebActivationError("live activation requires TERMDECK_ACTIVATION_APPROVED=1 after review")
    before = snapshot_database(paths, tx, "before")
    receipt["database_before"] = before
    receipt["processes_before"] = process_snapshot()
    receipt["preimages"] = {}
    for relative in _sources(paths, tx):
        receipt["preimages"]["runtime:" + relative] = _preimage(paths.runtime / relative, tx, "runtime-" + relative.replace("/", "-"))
    for relative in GENERATED_RUNTIME:
        receipt["preimages"]["generated:" + relative] = _preimage(paths.runtime / relative, tx, "generated-" + relative.replace("/", "-"))
    for unit in (*SERVICES, WATCHDOG_UNIT):
        receipt["preimages"]["unit:" + unit] = _preimage(paths.unit_dir / unit, tx, "unit-" + unit)
    receipt["old_release_id"] = _managed_release_id(paths)
    receipt["package_before"] = {"current_release_id": receipt["old_release_id"]}
    _persist(paths, tx, receipt, "mutation-start")
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(10):
            _persist(paths, tx, receipt)

    thread = threading.Thread(target=heartbeat, daemon=True)
    try:
        thread.start()
        _persist(paths, tx, receipt, "watchdog-installing")
        _arm(paths, tx)
        receipt["watchdog"] = {"unit": WATCHDOG_UNIT, "armed_before_disruption": True}
        _persist(paths, tx, receipt, "watchdog-armed")
        package = receipt.get("package")
        if not isinstance(package, Mapping):
            raise WebActivationError("receipt lacks qualified package")
        _persist(paths, tx, receipt, "package-installing")
        receipt["package_result"] = _install(paths, package)
        _persist(paths, tx, receipt, "package-installed")
        _persist(paths, tx, receipt, "runtime-publishing")
        for relative, (_source_path, mode) in _sources(paths, tx).items():
            _copy(tx / "staged-runtime" / relative, paths.runtime / relative, mode)
        _publish_units(paths, tx)
        receipt["service_units_published"] = True
        _systemd("daemon-reload")
        _persist(paths, tx, receipt, "runtime-staged")
        _persist(paths, tx, receipt, "services-stopping")
        receipt["services_stopped"] = True
        _persist(paths, tx, receipt)
        _stop_web()
        _persist(paths, tx, receipt, "services-stopped")
        _persist(paths, tx, receipt, "services-starting")
        _start_web()
        receipt["health"] = _health()
        receipt["post_checks"] = {"database": verify_database(paths, tx, before), "processes": process_observation(receipt["processes_before"])}
        _persist(paths, tx, receipt, "activated")
        _disarm()
        receipt["watchdog"]["armed_after_activation"] = False
        _persist(paths, tx, receipt)
        return {"ok": True, "action": "activated", "transaction": str(tx), "receipt": receipt}
    except Exception as exc:
        if thread.is_alive():
            stop_heartbeat.set()
            thread.join()
        receipt["error"] = str(exc)
        _persist(paths, tx, receipt, "failed")
        try:
            rollback(paths, tx, reason=str(exc))
        except Exception as rollback_error:
            receipt["rollback_error"] = str(rollback_error)
            _persist(paths, tx, receipt, "rollback-failed")
            _atomic(paths.recovery_required, (str(tx) + "\n").encode())
        raise
    finally:
        stop_heartbeat.set()
        if thread.is_alive():
            thread.join()


def rollback(paths: ActivationPaths, tx: Path, *, reason: str) -> dict[str, object]:
    tx, receipt = load_transaction(paths, tx)
    if not receipt.get("mutation_started"):
        raise WebActivationError("rollback is not needed before the first mutation")
    _persist(paths, tx, receipt, "rollback-start")
    try:
        current_release_id = _managed_release_id(paths)
        old_release_id = receipt.get("old_release_id")
        receipt["package_rollback_required"] = current_release_id != old_release_id
        receipt["package_current_release_id"] = current_release_id
        _persist(paths, tx, receipt)
        if receipt.get("services_stopped"):
            _stop_web()
        preimages = receipt.get("preimages")
        if not isinstance(preimages, Mapping):
            raise WebActivationError("receipt lacks rollback preimages")
        for record in reversed(list(preimages.values())):
            if isinstance(record, Mapping):
                _restore(record, tx)
        _systemd("daemon-reload", check=False)
        if current_release_id != old_release_id:
            _rollback_package(paths, old_release_id)
            if _managed_release_id(paths) != old_release_id:
                raise WebActivationError("managed package identity did not return to its pre-activation release")
        _start_web()
        receipt["rollback_reason"] = reason
        receipt["post_checks"] = {"database": verify_database(paths, tx, receipt.get("database_before", {})), "processes": process_observation(receipt.get("processes_before", []))}
        _persist(paths, tx, receipt, "rolled-back")
        _disarm()
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
        heartbeat = float(receipt.get("heartbeat_epoch", 0) or 0)
        if receipt.get("mutation_started") and heartbeat and time.time() - heartbeat > WATCHDOG_TIMEOUT:
            with _try_activation_lock(paths) as acquired:
                if acquired:
                    # Re-read after acquiring the lock so a live activation that
                    # refreshed its receipt cannot be mistaken for a stale owner.
                    locked_tx, locked_receipt = load_transaction(paths, tx)
                    locked_heartbeat = float(locked_receipt.get("heartbeat_epoch", 0) or 0)
                    if (
                        locked_receipt.get("mutation_started")
                        and locked_heartbeat
                        and time.time() - locked_heartbeat > WATCHDOG_TIMEOUT
                    ):
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
    for name in ("target", "evidence", "runtime", "database", "managed-root", "managed-bin", "unit-dir"):
        parser.add_argument("--" + name)
    args = parser.parse_args(argv)
    try:
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
    except WebActivationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
