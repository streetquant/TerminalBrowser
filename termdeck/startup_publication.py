"""Publish the saved-terminal recovery worker without replacing web services.

This separate user service follows the backend lifecycle. Its immutable Python
payload is verified before imports; existing releases, TLS and session data are
not deployment inputs. File publication uses the established descriptor-based
transaction writer and retains an exact, guarded inverse.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

from .release_manager import ReleaseError, _publish_files_transactionally


SERVICE = "termdeck-saved-terminals.service"
SCHEMA = "termdeck.startup-publication.v1"
SELECTORS = ("CODEX_HOME", "CODEX_AUTH_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
             "OPENAI_API_BASE", "CODEX_SQLITE_HOME")


class StartupPublicationError(RuntimeError):
    """Source, publication or activation no longer matches reviewed inputs."""


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _object(content: bytes) -> dict:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise StartupPublicationError("publication metadata is malformed") from exc
    if not isinstance(value, dict):
        raise StartupPublicationError("publication metadata is not an object")
    return value


def _physical(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.resolve(strict=True) != path:
        raise StartupPublicationError("publication path has a symlink ancestor")
    return path


def _directory(path: Path, *, create=False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _physical(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise StartupPublicationError("publication directory has unsafe ownership or mode")
    return path


def _read(path: Path, *, absent=False):
    if absent and not os.path.lexists(path):
        return {"exists": False, "content": b"", "mode": 0}
    _physical(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022):
            raise StartupPublicationError("publication file has unsafe ownership or mode")
        chunks = []
        while chunk := os.read(fd, 131072):
            chunks.append(chunk)
        content = b"".join(chunks)
        after = os.fstat(fd)
        names = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                 "st_size", "st_mtime_ns", "st_ctime_ns")
        named = path.lstat()
        if any(getattr(before, name) != getattr(after, name)
               or getattr(after, name) != getattr(named, name) for name in names):
            raise StartupPublicationError("publication file changed while reading")
        return {"exists": True, "content": content, "mode": stat.S_IMODE(after.st_mode),
                "device": after.st_dev, "inode": after.st_ino, "nlink": after.st_nlink,
                "size": after.st_size, "mtime_ns": after.st_mtime_ns,
                "ctime_ns": after.st_ctime_ns, "uid": before.st_uid,
                "gid": before.st_gid, "atime_ns": before.st_atime_ns,
                "sha256": _digest(content)}
    finally:
        os.close(fd)


def _write_new(path: Path, content: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    # Preserve an incomplete new leaf for inspection, never overwrite it.
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _source_files(source: Path):
    package = _directory(source / "termdeck")
    result = {}
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        _physical(path.parent)
        result[relative] = _read(path)
    if "termdeck/startup_recovery.py" not in result:
        raise StartupPublicationError("source lacks saved-terminal recovery")
    return result


def _launcher(site: Path, entries: dict) -> bytes:
    # No credential values or private terminal commands belong in this file.
    return ("import hashlib, os, pathlib, stat, sys\n"
            f"root = pathlib.Path({str(site)!r})\n"
            f"entries = {entries!r}\n"
            "if root.resolve(strict=True) != root: raise RuntimeError('worker root moved')\n"
            "for relative, expected in entries.items():\n"
            "    path = root / relative\n"
            "    if path.resolve(strict=True) != path: raise RuntimeError('worker path moved')\n"
            "    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)\n"
            "    try:\n"
            "        before = os.fstat(fd)\n"
            "        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid() or before.st_mode & 0o222: raise RuntimeError('unsafe worker module')\n"
            "        content = bytearray()\n"
            "        while chunk := os.read(fd, 131072): content.extend(chunk)\n"
            "        after = os.fstat(fd)\n"
            "        named = path.lstat()\n"
            "        keys = ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')\n"
            "        if any(getattr(before,k) != getattr(after,k) or getattr(after,k) != getattr(named,k) for k in keys): raise RuntimeError('worker module changed')\n"
            "        if len(content) != expected['size'] or hashlib.sha256(content).hexdigest() != expected['sha256']: raise RuntimeError('worker module mismatch')\n"
            "    finally: os.close(fd)\n"
            "if sys.argv[1:] == ['--verify-only']: raise SystemExit(0)\n"
            "sys.path.insert(0, str(root))\n"
            "if sys.argv[1:2] == ['--record-output']:\n"
            "    from termdeck.output_journal import pipe_main\n"
            "    raise SystemExit(pipe_main(sys.argv[2:]))\n"
            "if sys.argv[1:2] == ['--mirror-recordings']:\n"
            "    from termdeck.output_journal import mirror_main\n"
            "    raise SystemExit(mirror_main(sys.argv[2:]))\n"
            "from termdeck.startup_recovery import main\n"
            "raise SystemExit(main())\n").encode()


def prepare(source: Path, managed_root: Path) -> dict:
    """Freeze exact physical source bytes; do not import or run the worker."""
    source = _directory(source)
    managed_root = _directory(managed_root)
    files = _source_files(source)
    entries = {name: {"sha256": item["sha256"], "size": item["size"]}
               for name, item in files.items()}
    bundle_id = _digest(_json(entries))
    base = _directory(managed_root / "startup-recovery", create=True)
    releases = _directory(base / "releases", create=True)
    destination = releases / bundle_id
    if os.path.lexists(destination):
        verify_bundle(destination, entries)
        return {"bundle_id": bundle_id, "bundle": str(destination), "files": entries}
    stage = releases / (".stage-" + uuid4().hex)
    _directory(stage, create=True)
    site = _directory(stage / "site", create=True)
    for relative, item in files.items():
        target = site / relative
        _directory(target.parent, create=True)
        _write_new(target, item["content"], 0o444)
    # Source drift, including newly added modules, cannot publish a mixed tree.
    fresh = _source_files(source)
    identity_keys = ("exists", "content", "mode", "device", "inode", "nlink", "size",
                     "mtime_ns", "ctime_ns", "sha256", "uid", "gid")
    if (set(fresh) != set(files) or any(fresh[name][key] != files[name][key]
            for name in files for key in identity_keys)):
        raise StartupPublicationError("source changed during worker preparation")
    final_site = destination / "site"
    launcher = _launcher(final_site, entries)
    _write_new(stage / "launch.py", launcher, 0o444)
    _write_new(stage / "manifest.json", _json({"schema": SCHEMA, "bundle_id": bundle_id,
        "files": entries, "launcher_sha256": _digest(launcher)}), 0o444)
    directories = sorted((p for p in stage.rglob("*") if p.is_dir()),
                         key=lambda path: len(path.parts), reverse=True)
    for path in directories + [stage]:
        _sync(path)
        path.chmod(0o555)
    # An existing destination is never replaced, even by an equal digest.
    from .release_manager import _renameat2, _RENAME_NOREPLACE
    descriptor = os.open(releases, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        named = releases.lstat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise StartupPublicationError("worker release directory changed")
        _renameat2(descriptor, stage.name, destination.name,
                   flags=_RENAME_NOREPLACE, label="immutable recovery payload")
    finally:
        os.close(descriptor)
    _sync(releases)
    verify_bundle(destination, entries)
    return {"bundle_id": bundle_id, "bundle": str(destination), "files": entries}


def verify_bundle(bundle: Path, entries=None) -> dict:
    bundle = _directory(bundle)
    if bundle.lstat().st_mode & 0o222:
        raise StartupPublicationError("worker bundle is writable")
    manifest = _object(_read(bundle / "manifest.json")["content"])
    if manifest.get("schema") != SCHEMA:
        raise StartupPublicationError("worker manifest schema mismatch")
    actual_entries = manifest.get("files")
    if (not isinstance(actual_entries, dict) or not actual_entries
            or manifest.get("bundle_id") != _digest(_json(actual_entries))
            or bundle.name != manifest["bundle_id"]
            or (entries is not None and actual_entries != entries)):
        raise StartupPublicationError("worker source identity mismatch")
    site = bundle / "site"
    if set(_source_files(site)) != set(actual_entries):
        raise StartupPublicationError("worker module set mismatch")
    for relative, expected in actual_entries.items():
        item = _read(site / relative)
        if (item["sha256"] != expected.get("sha256") or item["size"] != expected.get("size")
                or item["mode"] & 0o222):
            raise StartupPublicationError("worker module identity mismatch")
    launcher = _read(bundle / "launch.py")
    if (launcher["mode"] & 0o222 or launcher["content"] != _launcher(site, actual_entries)
            or launcher["sha256"] != manifest.get("launcher_sha256")):
        raise StartupPublicationError("worker launcher identity mismatch")
    return manifest


def _unit(bundle: Path, managed: Path, state: Path, *,
          deferred_native_accounts=()) -> bytes:
    def argument(value):
        text = str(value)
        if any(character in text for character in ('\n', '\r', '\x00')):
            raise StartupPublicationError("unit argument contains control bytes")
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    if (not isinstance(deferred_native_accounts, (list, tuple))
            or any(not isinstance(name, str) or not name.strip()
                   for name in deferred_native_accounts)):
        raise StartupPublicationError("deferred native account list is invalid")
    argv = ["/usr/bin/python3", "-I", "-B", bundle / "launch.py", "--watch", "--db",
            state / "sessions.sqlite3", "--managed-root", managed]
    for name in deferred_native_accounts:
        argv.extend(("--defer-native-account", name))
    command = " ".join(argument(value) for value in argv)
    owner = pwd.getpwuid(os.geteuid())
    tool_path = f"{owner.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    return ("[Unit]\nDescription=Restore saved TermDeck terminals\n"
        "After=termdeck-web-backend.service\nPartOf=termdeck-web-backend.service\n"
        "BindsTo=termdeck-web-backend.service\n"
        "[Service]\nType=simple\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=15\n"
        "KillMode=control-group\n"
        f"UnsetEnvironment={' '.join(SELECTORS)}\n"
        f"Environment=HOME={argument(owner.pw_dir)}\n"
        f"Environment=USER={argument(owner.pw_name)}\n"
        f"Environment=LOGNAME={argument(owner.pw_name)}\n"
        f"Environment=SHELL={argument(owner.pw_shell)}\n"
        f"Environment=PATH={argument(tool_path)}\n"
        f"Environment=TERMDECK_STATE_ROOT={argument(state)}\n"
        f"Environment=TERMDECK_TMUX_RUNTIME={argument(state / 'tmux')}\n"
        f"Environment=TMUX_TMPDIR={argument(state / 'tmux')}\n"
        f"ExecStart={command}\n[Install]\nWantedBy=termdeck-web-backend.service\n").encode()


@contextmanager
def _lock(base: Path):
    path = base / "publication.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise StartupPublicationError("publication lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _encode_snapshot(snapshot):
    return dict(snapshot, content_base64=base64.b64encode(snapshot["content"]).decode(),
                content=None)


def _decode_snapshot(snapshot):
    snapshot = dict(snapshot)
    snapshot["content"] = base64.b64decode(snapshot.pop("content_base64"), validate=True)
    return snapshot


def publish(bundle: Path, managed_root: Path, state_root: Path, unit_dir: Path,
            backup_path: Path, *, deferred_native_accounts=()) -> dict:
    """Atomically install only this unit/receipt, with exact original preimages."""
    manifest = verify_bundle(bundle)
    managed = _directory(managed_root)
    state = _directory(state_root)
    units = _directory(unit_dir)
    base = _directory(managed / "startup-recovery")
    if bundle.parent != base / "releases":
        raise StartupPublicationError("bundle belongs to a different managed root")
    _directory(backup_path.parent)
    unit_path = units / SERVICE
    selected = base / "selection.json"
    with _lock(base):
        preimages = {path: _read(path, absent=True) for path in (unit_path, selected)}
        if preimages[unit_path]["exists"] or preimages[selected]["exists"]:
            if not all(item["exists"] for item in preimages.values()):
                raise StartupPublicationError("partial or foreign startup publication")
            previous = _object(preimages[selected]["content"])
            if (previous.get("schema") != SCHEMA or previous.get("unit_path") != str(unit_path)
                    or previous.get("unit_sha256") != preimages[unit_path]["sha256"]):
                raise StartupPublicationError("existing startup unit is not owned")
        unit = _unit(bundle, managed, state,
                     deferred_native_accounts=deferred_native_accounts)
        selection = _json({"schema": SCHEMA, "bundle_id": manifest["bundle_id"],
            "bundle": str(bundle), "state_root": str(state),
            "unit_path": str(unit_path), "unit_sha256": _digest(unit),
            "deferred_native_accounts": list(deferred_native_accounts)})
        inverse = {"schema": SCHEMA, "preimages": {str(path): _encode_snapshot(item)
            for path, item in preimages.items()}, "postimages": {
                str(unit_path): {"sha256": _digest(unit), "mode": 0o644},
                str(selected): {"sha256": _digest(selection), "mode": 0o600}}}
        _write_new(backup_path, _json(inverse), 0o600)
        _sync(backup_path.parent)
        def verify():
            verify_bundle(bundle)
            if (_read(unit_path)["content"] != unit or _read(selected)["content"] != selection):
                raise StartupPublicationError("startup publication readback mismatch")
        _publish_files_transactionally(((unit_path, unit, 0o644, "saved terminal unit"),
            (selected, selection, 0o600, "saved terminal selection")), verify=verify,
            expected_preimages=preimages, journal_path=base / ".termdeck-publication-startup.json")
    return {"schema": SCHEMA, "state": "published", "bundle_id": manifest["bundle_id"],
            "unit_path": str(unit_path), "unit_sha256": _digest(unit),
            "backup_path": str(backup_path), "backup_sha256": _read(backup_path)["sha256"]}


def rollback(backup_path: Path, managed_root: Path, unit_dir: Path) -> dict:
    """Restore only an exact owned postimage; newer state always blocks."""
    base = _directory(managed_root / "startup-recovery")
    units = _directory(unit_dir)
    backup = _object(_read(backup_path)["content"])
    targets = {units / SERVICE, base / "selection.json"}
    if (backup.get("schema") != SCHEMA or set(backup.get("preimages", {})) != {str(p) for p in targets}
            or set(backup.get("postimages", {})) != {str(p) for p in targets}):
        raise StartupPublicationError("rollback targets are invalid")
    with _lock(base):
        current = {path: _read(path) for path in targets}
        for path, item in current.items():
            expected = backup["postimages"][str(path)]
            if item["sha256"] != expected.get("sha256") or item["mode"] != expected.get("mode"):
                raise StartupPublicationError("startup state changed after publication")
        preimages = {path: _decode_snapshot(backup["preimages"][str(path)]) for path in targets}
        writes = tuple((path, item["content"] if item["exists"] else None,
                        item["mode"], "saved terminal inverse") for path, item in preimages.items())
        def verify():
            for path, item in preimages.items():
                observed = _read(path, absent=True)
                if any(observed[key] != item[key] for key in ("exists", "content", "mode")):
                    raise StartupPublicationError("startup inverse readback mismatch")
        _publish_files_transactionally(writes, verify=verify, expected_preimages=current,
            journal_path=base / ".termdeck-publication-startup.json")
    return {"schema": SCHEMA, "state": "rolled-back", "backup_path": str(backup_path)}


def activate(managed_root: Path, unit_dir: Path, *, run=subprocess.run) -> None:
    """Ask the owning user manager to load/enable this unit, never restart web."""
    managed = _directory(managed_root)
    base = _directory(managed / "startup-recovery")
    units = _directory(unit_dir)
    unit_path = units / SERVICE
    with _lock(base):
        selection = _object(_read(base / "selection.json")["content"])
        if selection.get("schema") != SCHEMA or selection.get("unit_path") != str(unit_path):
            raise StartupPublicationError("startup activation selection mismatch")
        bundle = Path(selection["bundle"])
        if bundle.parent != base / "releases":
            raise StartupPublicationError("startup activation bundle is foreign")
        manifest = verify_bundle(bundle)
        state = _directory(Path(selection["state_root"]))
        unit = _unit(bundle, managed, state,
                     deferred_native_accounts=selection.get("deferred_native_accounts", []))
        original = _read(unit_path)
        if (selection.get("bundle_id") != manifest["bundle_id"]
                or selection.get("unit_sha256") != _digest(unit)
                or original["content"] != unit or original["mode"] != 0o644):
            raise StartupPublicationError("startup activation unit mismatch")
        commands = (("daemon-reload",), ("show", SERVICE, "-p", "FragmentPath", "--value"),
                    ("enable", "--now", SERVICE), ("is-enabled", SERVICE), ("is-active", SERVICE))
        for arguments in commands:
            current = _read(unit_path)
            if any(current[key] != original[key] for key in
                   ("content", "mode", "device", "inode", "mtime_ns", "ctime_ns")):
                raise StartupPublicationError("startup unit changed during activation")
            result = run(("/usr/bin/systemctl", "--user", *arguments),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise StartupPublicationError("user-manager startup activation failed")
            if arguments[0] == "show" and result.stdout.strip() != os.fsencode(unit_path):
                raise StartupPublicationError("user manager selected a different unit")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "publish", "activate", "rollback"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--managed-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--unit-dir", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--defer-native-account", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            if args.source is None:
                parser.error("prepare requires --source")
            result = prepare(args.source, args.managed_root)
        elif args.action == "publish":
            if any(value is None for value in (args.bundle, args.state_root, args.unit_dir, args.backup)):
                parser.error("publish requires --bundle --state-root --unit-dir --backup")
            result = publish(args.bundle, args.managed_root, args.state_root, args.unit_dir,
                             args.backup, deferred_native_accounts=args.defer_native_account)
        elif args.action == "rollback":
            if args.backup is None or args.unit_dir is None:
                parser.error("rollback requires --unit-dir --backup")
            result = rollback(args.backup, args.managed_root, args.unit_dir)
        else:
            if args.unit_dir is None:
                parser.error("activate requires --unit-dir")
            activate(args.managed_root, args.unit_dir)
            result = {"schema": SCHEMA, "state": "active", "unit": SERVICE}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, ReleaseError, StartupPublicationError) as exc:
        print(json.dumps({"schema": SCHEMA, "state": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
