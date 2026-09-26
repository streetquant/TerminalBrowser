#!/usr/bin/env python3.14
from __future__ import annotations
import atexit
import base64
import csv
import hashlib
import io
import importlib.abc
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path('/warp/runtime/termdeck/t1000-managed')
STATE = ROOT / "state.json"
RELEASE_VERIFICATION_SCHEMA = "t1000.release-verification.v2"
SITE_MANIFEST_SCHEMA = "t1000.installed-site-manifest.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SITE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RELEASE_VERIFICATION_BYTES = MAX_SITE_MANIFEST_BYTES + 64 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_BYTES = 512 * 1024 * 1024
MAX_WHEEL_ENTRIES = 20_000
MAX_WHEEL_PATH_COMPONENTS = 256
MAX_SITE_DIRECTORIES = 100_000
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def digest_file(path, *, maximum_bytes):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        entry = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"cannot hash a non-regular file: {path}")
    if entry.st_size < 0 or entry.st_size > maximum_bytes:
        os.close(descriptor)
        raise RuntimeError(f"file exceeds the {maximum_bytes}-byte digest limit: {path}")
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        remaining = entry.st_size
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError(f"file changed while being hashed: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise RuntimeError(f"file grew while being hashed: {path}")
    after = os.stat(path, follow_symlinks=False)
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        raise RuntimeError(f"file changed while being hashed: {path}")
    return digest.hexdigest()


def regular_file(path, label, *, immutable=False, executable=False):
    entry = path.lstat()
    mode = entry.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")
    permissions = stat.S_IMODE(mode)
    if immutable and (permissions & 0o222 or entry.st_nlink != 1):
        raise RuntimeError(f"{label} is writable or hard-linked: {path}")
    if executable and (permissions & 0o100 == 0 or permissions & 0o022):
        raise RuntimeError(f"{label} has unsafe executable permissions: {path}")
    return path


def bounded_bytes(path, label, maximum_bytes):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        entry = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    mode = entry.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        os.close(descriptor)
        raise RuntimeError(f"{label} is not a regular file: {path}")
    if entry.st_size < 0 or entry.st_size > maximum_bytes:
        os.close(descriptor)
        raise RuntimeError(f"{label} exceeds the {maximum_bytes}-byte safety limit")
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        value = handle.read(entry.st_size)
        if len(value) != entry.st_size or handle.read(1):
            raise RuntimeError(f"{label} changed while being read")
    after = path.lstat()
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        raise RuntimeError(f"{label} changed while being read")
    return value


def real_directory(path, label):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"{label} is not a real directory: {path}")
    return path.resolve(strict=True)


def exact_directory_entries(path, label, expected):
    names = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) > len(expected):
                    raise RuntimeError(
                        f"{label} contains unexpected entries outside the immutable release envelope"
                    )
    except OSError as exc:
        raise RuntimeError(f"could not enumerate {label}: {exc}") from exc
    if names != set(expected):
        raise RuntimeError(f"{label} entries differ from the immutable release envelope")


def frozen_directories_required(build_info):
    version = build_info.get("version") if isinstance(build_info, dict) else None
    if not isinstance(version, str):
        return True
    match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[A-Za-z0-9.+-].*)?",
        version,
    )
    if match is None:
        return True
    try:
        numeric = tuple(int(component) for component in match.groups())
    except ValueError:
        return True
    return numeric >= (0, 2, 13)


def safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe wheel member name: {name!r}")
    trailing = name.endswith("/")
    normalized = name[:-1] if trailing else name
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or len(parts) > MAX_WHEEL_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RuntimeError(f"unsafe wheel member path: {name!r}")
    if "/".join(parts) != normalized:
        raise RuntimeError(f"non-canonical wheel member path: {name!r}")
    return normalized


def record_digest_hex(value, name):
    match = re.fullmatch(r"sha256=([A-Za-z0-9_-]{43})", value)
    if match is None:
        raise RuntimeError(f"wheel RECORD SHA-256 is invalid: {name}")
    decoded = base64.urlsafe_b64decode(match.group(1) + "=")
    if len(decoded) != hashlib.sha256().digest_size:
        raise RuntimeError(f"wheel RECORD SHA-256 is invalid: {name}")
    return decoded.hex()


def validate_site_manifest(value, *, release_id, wheel_sha256):
    if not isinstance(value, dict):
        raise RuntimeError("installed site manifest is not an object")
    expected_fields = {"schema", "release_id", "wheel_sha256", "directories", "files"}
    if set(value) != expected_fields:
        raise RuntimeError("installed site manifest fields differ from schema")
    if value["schema"] != SITE_MANIFEST_SCHEMA:
        raise RuntimeError("installed site manifest schema is unsupported")
    if value["release_id"] != release_id:
        raise RuntimeError("installed site manifest release id mismatch")
    if value["wheel_sha256"] != wheel_sha256:
        raise RuntimeError("installed site manifest wheel mismatch")
    directories = value["directories"]
    if not isinstance(directories, list) or not directories or len(directories) > MAX_SITE_DIRECTORIES:
        raise RuntimeError("installed site manifest directory count is unsafe")
    if any(not isinstance(item, str) for item in directories):
        raise RuntimeError("installed site manifest directory path is invalid")
    if directories != sorted(set(directories)) or "." not in directories:
        raise RuntimeError("installed site manifest directories are not canonical")
    directory_set = set(directories)
    for name in directories:
        if name == ".":
            continue
        safe_member(name)
        if PurePosixPath(name).parent.as_posix() not in directory_set:
            raise RuntimeError("installed site manifest directory parent is missing")
    files = value["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_WHEEL_ENTRIES:
        raise RuntimeError("installed site manifest file count is unsafe")
    names = []
    total_size = 0
    for row in files:
        if not isinstance(row, list) or len(row) != 3:
            raise RuntimeError("installed site manifest file row is invalid")
        name, digest, size = row
        if not isinstance(name, str):
            raise RuntimeError("installed site manifest file path is invalid")
        safe_member(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError(f"installed site manifest digest is invalid: {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"installed site manifest size is invalid: {name}")
        if PurePosixPath(name).parent.as_posix() not in directory_set:
            raise RuntimeError(f"installed site manifest file parent is missing: {name}")
        total_size += size
        if total_size > MAX_WHEEL_BYTES:
            raise RuntimeError("installed site manifest total size is unsafe")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise RuntimeError("installed site manifest file paths are not canonical")
    if len(canonical(value)) > MAX_SITE_MANIFEST_BYTES:
        raise RuntimeError("installed site manifest exceeds the safety limit")
    return value


def site_manifest_from_wheel(path, *, release_id, expected_sha256, expected_size):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o222:
            raise RuntimeError("managed wheel is writable, linked, or unsafe")
        if before.st_size != expected_size or before.st_size > MAX_WHEEL_BYTES:
            raise RuntimeError("managed wheel artifact failed size verification")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("managed wheel changed while being hashed")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1) or digest.hexdigest() != expected_sha256:
                raise RuntimeError("managed wheel artifact failed size/SHA-256 verification")
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > MAX_WHEEL_ENTRIES:
                    raise RuntimeError("managed wheel entry count is unsafe")
                names = set()
                files = set()
                file_infos = {}
                directories = {"."}
                total_size = 0
                for info in infos:
                    name = safe_member(info.filename)
                    if name in names:
                        raise RuntimeError(f"managed wheel contains duplicate member: {name}")
                    names.add(name)
                    if info.flag_bits & 0x1 or info.file_size < 0 or info.file_size > 128 * 1024 * 1024:
                        raise RuntimeError(f"managed wheel member is unsafe: {name}")
                    total_size += info.file_size
                    if total_size > MAX_WHEEL_BYTES:
                        raise RuntimeError("managed wheel uncompressed size exceeds safety limit")
                    if info.is_dir():
                        directories.add(name)
                    else:
                        files.add(name)
                        file_infos[name] = info
                    parent = PurePosixPath(name).parent
                    while parent.as_posix() != ".":
                        directories.add(parent.as_posix())
                        if len(directories) > MAX_SITE_DIRECTORIES:
                            raise RuntimeError("managed wheel directory count is unsafe")
                        parent = parent.parent
                records = sorted(name for name in files if name.endswith(".dist-info/RECORD"))
                if len(records) != 1:
                    raise RuntimeError("managed wheel must contain exactly one RECORD")
                record_name = records[0]
                if file_infos[record_name].file_size > MAX_SITE_MANIFEST_BYTES:
                    raise RuntimeError("managed wheel RECORD exceeds the safety limit")
                record_bytes = archive.read(record_name)
                if len(record_bytes) > MAX_SITE_MANIFEST_BYTES:
                    raise RuntimeError("managed wheel RECORD exceeds the safety limit")
                pinned_members = {}
                for name in sorted(files):
                    info = file_infos[name]
                    content = archive.read(info)
                    if len(content) != info.file_size:
                        raise RuntimeError(f"managed wheel member changed size: {name}")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    pinned_members[name] = (content, bool(mode & 0o111))
        try:
            record_text = record_bytes.decode("utf-8")
            rows = list(csv.reader(io.StringIO(record_text)))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise RuntimeError(f"managed wheel RECORD is invalid: {exc}") from exc
        record_map = {}
        for row in rows:
            if len(row) != 3:
                raise RuntimeError("managed wheel RECORD row is malformed")
            name, digest_value, size_text = row
            name = safe_member(name)
            if name in record_map:
                raise RuntimeError(f"managed wheel RECORD path is duplicated: {name}")
            record_map[name] = (digest_value, size_text)
        if set(record_map) != files:
            raise RuntimeError("managed wheel RECORD coverage differs from archive")
        file_rows = []
        for name in sorted(files):
            digest_value, size_text = record_map[name]
            if name == record_name:
                if digest_value or size_text:
                    raise RuntimeError("managed wheel RECORD self-row is not empty")
                file_rows.append([name, digest_bytes(record_bytes), len(record_bytes)])
                continue
            if not size_text.isdecimal() or str(int(size_text)) != size_text:
                raise RuntimeError(f"managed wheel RECORD size is invalid: {name}")
            file_rows.append([name, record_digest_hex(digest_value, name), int(size_text)])
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        identity = lambda entry: (
            entry.st_dev,
            entry.st_ino,
            entry.st_size,
            entry.st_mtime_ns,
            entry.st_ctime_ns,
        )
        if identity(after) != identity(before) or identity(path_after) != identity(before):
            raise RuntimeError("managed wheel changed while being verified")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"managed wheel is invalid: {exc}") from exc
    finally:
        os.close(descriptor)
    return (
        validate_site_manifest(
            {
                "schema": SITE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "wheel_sha256": expected_sha256,
                "directories": sorted(directories),
                "files": file_rows,
            },
            release_id=release_id,
            wheel_sha256=expected_sha256,
        ),
        pinned_members,
    )


def verify_installed_site(site, site_manifest, *, frozen):
    site_root = site.resolve(strict=True)
    directories = set(site_manifest["directories"])
    children = {name: {} for name in directories}
    for name in directories:
        if name == ".":
            continue
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"installed site path collision: {name}")
        children[parent][child] = "directory"
    for name, _digest, _size in site_manifest["files"]:
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"installed site path collision: {name}")
        children[parent][child] = "file"
    for name in sorted(directories, key=lambda value: (value.count("/"), value)):
        target = site if name == "." else site.joinpath(*PurePosixPath(name).parts)
        entry = target.lstat()
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise RuntimeError(f"installed site directory is unsafe: {name}")
        target.resolve(strict=True).relative_to(site_root)
        if frozen and stat.S_IMODE(entry.st_mode) != 0o555:
            raise RuntimeError(f"installed site directory is not frozen: {name}")
        expected = children[name]
        observed = {}
        with os.scandir(target) as entries:
            for index, child in enumerate(entries):
                if index >= len(expected):
                    raise RuntimeError(f"installed site directory contains extra entries: {name}")
                if child.is_symlink():
                    raise RuntimeError(f"installed site contains a symlink: {child.path}")
                if child.is_dir(follow_symlinks=False):
                    observed[child.name] = "directory"
                elif child.is_file(follow_symlinks=False):
                    observed[child.name] = "file"
                else:
                    raise RuntimeError(f"installed site contains a special file: {child.path}")
        if observed != expected:
            raise RuntimeError(f"installed site directory entries mismatch: {name}")
    for name, expected_digest, expected_size in site_manifest["files"]:
        target = site.joinpath(*PurePosixPath(name).parts)
        entry = regular_file(target, "installed site member", immutable=True).lstat()
        target.resolve(strict=True).relative_to(site_root)
        if entry.st_size != expected_size:
            raise RuntimeError(f"installed site member size mismatch: {name}")
        if digest_file(target, maximum_bytes=expected_size) != expected_digest:
            raise RuntimeError(f"installed site member SHA-256 mismatch: {name}")


def snapshot_identity(entry):
    return (
        entry.st_dev,
        entry.st_ino,
        stat.S_IFMT(entry.st_mode),
        stat.S_IMODE(entry.st_mode),
        entry.st_nlink,
        entry.st_size,
    )


def discard_partial_snapshot(snapshot, descriptor, files, directories):
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    try:
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
            try:
                entry = target.lstat()
                if not stat.S_ISLNK(entry.st_mode) and stat.S_ISDIR(entry.st_mode):
                    target.chmod(0o700)
            except OSError:
                pass
        for name in reversed(files):
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            try:
                entry = target.lstat()
                if not stat.S_ISLNK(entry.st_mode) and stat.S_ISREG(entry.st_mode):
                    target.unlink()
            except OSError:
                pass
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            try:
                descriptor_root.joinpath(*PurePosixPath(name).parts).rmdir()
            except OSError:
                pass
        try:
            named = snapshot.lstat()
            opened = os.fstat(descriptor)
            if snapshot_identity(named) == snapshot_identity(opened):
                snapshot.rmdir()
        except OSError:
            pass
    finally:
        os.close(descriptor)


def cleanup_import_snapshot(snapshot, descriptor, files, directories, identities):
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    try:
        named_root = snapshot.lstat()
        opened_root = os.fstat(descriptor)
        if snapshot_identity(named_root) != identities["."]:
            return
        if snapshot_identity(opened_root) != identities["."]:
            return
        for name, expected in identities.items():
            if name == ".":
                continue
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            if snapshot_identity(target.lstat()) != expected:
                return
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
            target.chmod(0o700)
        for name in files:
            descriptor_root.joinpath(*PurePosixPath(name).parts).unlink()
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            descriptor_root.joinpath(*PurePosixPath(name).parts).rmdir()
        snapshot.rmdir()
    except OSError:
        pass
    finally:
        os.close(descriptor)


def verify_import_snapshot(
    snapshot,
    descriptor,
    files,
    directories,
    identities,
    *,
    expected_device,
):
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    opened_root = os.fstat(descriptor)
    named_root = snapshot.lstat()
    if snapshot_identity(opened_root) != identities["."]:
        raise RuntimeError("verified import snapshot root descriptor changed")
    if snapshot_identity(named_root) != identities["."]:
        raise RuntimeError("verified import snapshot root path changed")
    if opened_root.st_dev != expected_device:
        raise RuntimeError("verified import snapshot is not on the managed release device")
    if descriptor_root.resolve(strict=True) != snapshot.resolve(strict=True):
        raise RuntimeError("verified import snapshot descriptor path mismatch")

    children = {name: {} for name in directories}
    for name in directories:
        if name == ".":
            continue
        parent = PurePosixPath(name).parent.as_posix()
        children[parent][PurePosixPath(name).name] = "directory"
    for name in files:
        parent = PurePosixPath(name).parent.as_posix()
        child = PurePosixPath(name).name
        if child in children[parent]:
            raise RuntimeError(f"verified import snapshot path collision: {name}")
        children[parent][child] = "file"

    for name in sorted(directories, key=lambda value: (value.count("/"), value)):
        target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
        entry = os.fstat(descriptor) if name == "." else target.lstat()
        if snapshot_identity(entry) != identities[name]:
            raise RuntimeError(f"verified import snapshot directory changed: {name}")
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise RuntimeError(f"verified import snapshot directory is unsafe: {name}")
        if stat.S_IMODE(entry.st_mode) != 0o500:
            raise RuntimeError(f"verified import snapshot directory mode is unsafe: {name}")
        observed = {}
        with os.scandir(target) as entries:
            for index, child in enumerate(entries):
                if index >= len(children[name]):
                    raise RuntimeError(f"verified import snapshot directory has extra entries: {name}")
                if child.is_symlink():
                    raise RuntimeError(f"verified import snapshot contains a symlink: {child.path}")
                if child.is_dir(follow_symlinks=False):
                    observed[child.name] = "directory"
                elif child.is_file(follow_symlinks=False):
                    observed[child.name] = "file"
                else:
                    raise RuntimeError(f"verified import snapshot contains a special file: {child.path}")
        if observed != children[name]:
            raise RuntimeError(f"verified import snapshot directory entries mismatch: {name}")

    for name, (content, mode, digest) in files.items():
        target = descriptor_root.joinpath(*PurePosixPath(name).parts)
        entry = target.lstat()
        if snapshot_identity(entry) != identities[name]:
            raise RuntimeError(f"verified import snapshot member changed: {name}")
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise RuntimeError(f"verified import snapshot member is unsafe: {name}")
        if stat.S_IMODE(entry.st_mode) != mode or entry.st_nlink != 1:
            raise RuntimeError(f"verified import snapshot member mode is unsafe: {name}")
        if digest_file(target, maximum_bytes=len(content)) != digest:
            raise RuntimeError(f"verified import snapshot member digest mismatch: {name}")
    return descriptor_root


def materialize_import_snapshot(root, release, site_manifest, pinned_members):
    manifest_rows = {
        name: (digest, size)
        for name, digest, size in site_manifest["files"]
    }
    if set(pinned_members) != set(manifest_rows):
        raise RuntimeError("verified import bytes differ from the installed site descriptor")
    files = {}
    for name in sorted(pinned_members):
        content, executable = pinned_members[name]
        expected_digest, expected_size = manifest_rows[name]
        if len(content) != expected_size or digest_bytes(content) != expected_digest:
            raise RuntimeError(f"verified import bytes do not match the descriptor: {name}")
        files[name] = (
            content,
            0o500 if executable else 0o400,
            expected_digest,
        )
    directories = tuple(site_manifest["directories"])
    snapshot = Path(
        tempfile.mkdtemp(
            prefix=f".bootstrap-import-{os.getpid()}-",
            dir=root,
        )
    )
    try:
        descriptor = os.open(
            snapshot,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception:
        try:
            snapshot.rmdir()
        except OSError:
            pass
        raise
    descriptor_root = Path(f"/proc/self/fd/{descriptor}")
    created_directories = ["."]
    created_files = []
    try:
        for name in sorted(
            (value for value in directories if value != "."),
            key=lambda value: (value.count("/"), value),
        ):
            descriptor_root.joinpath(*PurePosixPath(name).parts).mkdir(mode=0o700)
            created_directories.append(name)
        for name, (content, mode, _digest) in files.items():
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            member_descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(member_descriptor, content[offset:])
                    if written <= 0:
                        raise RuntimeError(f"could not materialize verified import member: {name}")
                    offset += written
                os.fchmod(member_descriptor, mode)
            finally:
                os.close(member_descriptor)
            created_files.append(name)
        for name in sorted(
            created_directories,
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            target = descriptor_root if name == "." else descriptor_root.joinpath(*PurePosixPath(name).parts)
            target.chmod(0o500)
        identities = {".": snapshot_identity(os.fstat(descriptor))}
        for name in created_directories:
            if name == ".":
                continue
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            identities[name] = snapshot_identity(target.lstat())
        for name in created_files:
            target = descriptor_root.joinpath(*PurePosixPath(name).parts)
            identities[name] = snapshot_identity(target.lstat())
        import_root = verify_import_snapshot(
            snapshot,
            descriptor,
            files,
            directories,
            identities,
            expected_device=release.lstat().st_dev,
        )
    except Exception:
        discard_partial_snapshot(snapshot, descriptor, created_files, created_directories)
        raise
    return snapshot, descriptor, import_root, tuple(files), directories, identities


class SnapshotResourceReader:
    def __init__(self, root):
        self.root = root

    def _target(self, resource):
        if not isinstance(resource, str) or not resource or Path(resource).name != resource:
            raise FileNotFoundError(resource)
        return self.root / resource

    def open_resource(self, resource):
        return self._target(resource).open("rb")

    def resource_path(self, resource):
        return str(self._target(resource))

    def is_resource(self, resource):
        try:
            entry = self._target(resource).lstat()
        except OSError:
            return False
        return not stat.S_ISLNK(entry.st_mode) and stat.S_ISREG(entry.st_mode)

    def contents(self):
        with os.scandir(self.root) as entries:
            return tuple(entry.name for entry in entries)

    def files(self):
        return self.root


class VerifiedSnapshotImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root, pinned_members):
        self.root = root
        self.modules = {}
        for name, (content, _executable) in pinned_members.items():
            if not name.startswith("termdeck/") or not name.endswith(".py"):
                continue
            parts = list(PurePosixPath(name).parts)
            is_package = parts[-1] == "__init__.py"
            module_parts = parts[:-1] if is_package else parts[:-1] + [parts[-1][:-3]]
            if not module_parts or any(not part.isidentifier() for part in module_parts):
                continue
            fullname = ".".join(module_parts)
            if fullname in self.modules:
                raise RuntimeError(f"verified import module path collision: {fullname}")
            self.modules[fullname] = (name, content, is_package)
        package = self.modules.get("termdeck")
        if package is None or not package[2]:
            raise RuntimeError("verified import bytes have no termdeck package")

    def find_spec(self, fullname, path=None, target=None):
        record = self.modules.get(fullname)
        if record is None:
            if fullname == "termdeck" or fullname.startswith("termdeck."):
                raise ModuleNotFoundError(f"module is absent from verified import bytes: {fullname}")
            return None
        name, _content, is_package = record
        spec = importlib.util.spec_from_loader(
            fullname,
            self,
            is_package=is_package,
        )
        if spec is None:
            raise ImportError(f"could not create verified import spec: {fullname}")
        spec.cached = None
        if is_package:
            spec.submodule_search_locations = [
                str(self.root.joinpath(*PurePosixPath(name).parent.parts))
            ]
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name, content, _is_package = self.modules[module.__name__]
        filename = str(self.root.joinpath(*PurePosixPath(name).parts))
        code = compile(content, filename, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_filename(self, fullname):
        name, _content, _is_package = self.modules[fullname]
        return str(self.root.joinpath(*PurePosixPath(name).parts))

    def get_resource_reader(self, fullname):
        name, _content, is_package = self.modules.get(fullname, (None, None, False))
        if not is_package or not isinstance(name, str):
            return None
        return SnapshotResourceReader(
            self.root.joinpath(*PurePosixPath(name).parent.parts)
        )


def validate_release_verification(receipt, *, release_id, manifest, manifest_bytes):
    if not isinstance(receipt, dict):
        raise RuntimeError("release verification receipt is not an object")
    expected_fields = {
        "schema",
        "release_id",
        "manifest_sha256",
        "wheel_sha256",
        "wheel_size",
        "installed_members",
        "installed_directories",
        "site_directories",
        "frozen_directories",
        "site_manifest",
        "site_manifest_sha256",
        "method",
    }
    if set(receipt) != expected_fields:
        raise RuntimeError("release verification receipt fields differ from schema")
    if receipt["schema"] != RELEASE_VERIFICATION_SCHEMA:
        raise RuntimeError("release verification receipt schema is unsupported")
    if receipt["release_id"] != release_id:
        raise RuntimeError("release verification receipt release id mismatch")
    if receipt["manifest_sha256"] != digest_bytes(manifest_bytes):
        raise RuntimeError("release verification receipt manifest mismatch")
    if receipt["wheel_sha256"] != manifest.get("wheel_sha256"):
        raise RuntimeError("release verification receipt wheel mismatch")
    if receipt["wheel_size"] != manifest.get("wheel_size"):
        raise RuntimeError("release verification receipt wheel size mismatch")
    for field in ("manifest_sha256", "wheel_sha256"):
        if not isinstance(receipt[field], str) or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None:
            raise RuntimeError(f"release verification receipt {field} is invalid")
    for field in ("wheel_size", "installed_members", "installed_directories", "site_directories"):
        value = receipt[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"release verification receipt {field} is invalid")
    if receipt["installed_directories"] != receipt["site_directories"] + 2:
        raise RuntimeError("release verification receipt directory count mismatch")
    if not isinstance(receipt["frozen_directories"], bool):
        raise RuntimeError("release verification receipt frozen policy is invalid")
    if receipt["frozen_directories"] != frozen_directories_required(manifest.get("build_info")):
        raise RuntimeError("release verification receipt frozen policy mismatch")
    site_manifest = validate_site_manifest(
        receipt["site_manifest"],
        release_id=release_id,
        wheel_sha256=receipt["wheel_sha256"],
    )
    if len(site_manifest["files"]) != receipt["installed_members"]:
        raise RuntimeError("release verification receipt site member count mismatch")
    if len(site_manifest["directories"]) != receipt["site_directories"]:
        raise RuntimeError("release verification receipt site directory count mismatch")
    site_manifest_sha256 = receipt["site_manifest_sha256"]
    if not isinstance(site_manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", site_manifest_sha256) is None:
        raise RuntimeError("release verification receipt site manifest digest is invalid")
    if digest_bytes(canonical(site_manifest)) != site_manifest_sha256:
        raise RuntimeError("release verification receipt site manifest digest mismatch")
    if receipt["method"] != "full-records-site-manifest-v2":
        raise RuntimeError("release verification receipt method is unsupported")
    return site_manifest


try:
    regular_file(STATE, "managed state")
    raw_state = bounded_bytes(STATE, "managed state", MAX_MANIFEST_BYTES)
    state = json.loads(raw_state.decode("utf-8"))
    if canonical(state) != raw_state:
        raise RuntimeError("managed state is not canonical")
    state_fields = {
        "schema",
        "generation",
        "current_release_id",
        "previous_release_id",
        "manifest_sha256",
        "python_executable",
        "bootstrap_sha256",
        "launcher_path",
        "launcher_sha256",
        "legacy_launcher",
    }
    if set(state) != state_fields or state.get("schema") != "t1000.managed-state.v2":
        raise RuntimeError("managed state schema or fields mismatch")
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise RuntimeError("managed state generation is invalid")
    release_id = state.get("current_release_id")
    if not isinstance(release_id, str) or re.fullmatch(r"[0-9a-f]{64}", release_id) is None:
        raise RuntimeError("managed state has no valid current release")
    previous_id = state.get("previous_release_id")
    if previous_id is not None and (
        not isinstance(previous_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", previous_id) is None
        or previous_id == release_id
    ):
        raise RuntimeError("managed previous release is invalid")
    for field in ("manifest_sha256", "bootstrap_sha256", "launcher_sha256"):
        if not isinstance(state.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", state[field]) is None:
            raise RuntimeError(f"managed {field} is invalid")

    configured_python = Path(state.get("python_executable", ""))
    if not configured_python.is_absolute():
        raise RuntimeError("managed Python path is not absolute")
    regular_file(configured_python, "managed Python", executable=True)
    if configured_python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise RuntimeError("managed Python path does not match the running interpreter")
    bootstrap = regular_file(Path(__file__), "managed bootstrap", executable=True)
    expected_bootstrap = regular_file(
        ROOT / "bootstrap.py", "managed bootstrap", executable=True
    )
    if bootstrap.resolve(strict=True) != expected_bootstrap.resolve(strict=True):
        raise RuntimeError("managed bootstrap path mismatch")
    if digest_file(bootstrap, maximum_bytes=MAX_RUNTIME_BYTES) != state["bootstrap_sha256"]:
        raise RuntimeError("managed bootstrap SHA-256 mismatch")
    launcher = Path(state.get("launcher_path", ""))
    if not launcher.is_absolute():
        raise RuntimeError("managed launcher path is not absolute")
    regular_file(launcher, "managed launcher", executable=True)
    if digest_file(launcher, maximum_bytes=MAX_RUNTIME_BYTES) != state["launcher_sha256"]:
        raise RuntimeError("managed launcher SHA-256 mismatch")

    releases_root = real_directory(ROOT / "releases", "managed releases root")
    release_path = releases_root / release_id
    release = real_directory(release_path, "managed release")
    release.relative_to(releases_root)
    manifest_path = regular_file(
        release / "manifest.json", "managed manifest", immutable=True
    )
    manifest_bytes = bounded_bytes(manifest_path, "managed manifest", MAX_MANIFEST_BYTES)
    if digest_bytes(manifest_bytes) != state.get("manifest_sha256"):
        raise RuntimeError("managed manifest does not match state.json")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if canonical(manifest) != manifest_bytes:
        raise RuntimeError("managed manifest is not canonical")
    if manifest.get("schema") != "t1000.release-manifest.v1":
        raise RuntimeError("managed manifest schema mismatch")
    if manifest.get("release_id") != release_id:
        raise RuntimeError("managed manifest release_id mismatch")

    build_info = manifest.get("build_info")
    if not isinstance(build_info, dict):
        raise RuntimeError("managed manifest has no build info")
    if build_info.get("build_mode") != "release":
        raise RuntimeError("managed build is not a production release")
    build_fields = {
        key: build_info.get(key)
        for key in (
            "schema",
            "version",
            "source_commit",
            "source_tree_sha256",
            "source_date_epoch",
            "build_mode",
        )
    }
    if digest_bytes(canonical(build_fields)) != build_info.get("build_id"):
        raise RuntimeError("managed build_id mismatch")
    wheel_hash = manifest.get("wheel_sha256")
    if not isinstance(wheel_hash, str) or re.fullmatch(r"[0-9a-f]{64}", wheel_hash) is None:
        raise RuntimeError("managed wheel SHA-256 is invalid")
    wheel_size = manifest.get("wheel_size")
    if isinstance(wheel_size, bool) or not isinstance(wheel_size, int) or wheel_size <= 0:
        raise RuntimeError("managed wheel size is invalid")
    expected_release = digest_bytes(
        canonical({"build_id": build_info.get("build_id"), "wheel_sha256": wheel_hash})
    )
    if expected_release != release_id:
        raise RuntimeError("managed release_id does not bind build and wheel")

    wheel_name = manifest.get("wheel")
    if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
        raise RuntimeError("managed wheel name is unsafe")
    exact_directory_entries(
        release,
        "managed release",
        {"site", "artifacts", "manifest.json"},
    )
    artifacts = real_directory(release / "artifacts", "managed release artifacts")
    exact_directory_entries(
        artifacts,
        "managed release artifacts",
        {wheel_name},
    )
    wheel = regular_file(
        artifacts / wheel_name, "managed wheel", immutable=True
    )
    expected_site_manifest, pinned_members = site_manifest_from_wheel(
        wheel,
        release_id=release_id,
        expected_sha256=wheel_hash,
        expected_size=wheel_size,
    )

    site = real_directory(release / "site", "managed release site")
    if frozen_directories_required(build_info):
        for directory, label in (
            (release, "managed release"),
            (artifacts, "managed release artifacts"),
            (site, "managed release site"),
        ):
            if stat.S_IMODE(directory.lstat().st_mode) != 0o555:
                raise RuntimeError(f"{label} is not frozen")
    build_info_path = regular_file(
        site / "termdeck" / "_build_info.json",
        "bundled build info",
        immutable=True,
    )
    build_info_bytes = bounded_bytes(
        build_info_path,
        "bundled build info",
        MAX_MANIFEST_BYTES,
    )
    try:
        bundled_build_info = json.loads(build_info_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"bundled build info is invalid: {exc}") from exc
    if bundled_build_info != build_info:
        raise RuntimeError("bundled build info does not match the manifest")
    verification_path = ROOT / f"verification-{release_id}.json"
    regular_file(
        verification_path,
        "release verification receipt",
        immutable=True,
    )
    verification_bytes = bounded_bytes(
        verification_path,
        "release verification receipt",
        MAX_RELEASE_VERIFICATION_BYTES,
    )
    try:
        verification = json.loads(verification_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"release verification receipt is invalid: {exc}") from exc
    if canonical(verification) != verification_bytes:
        raise RuntimeError("release verification receipt is not canonical")
    site_manifest = validate_release_verification(
        verification,
        release_id=release_id,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
    )
    if site_manifest != expected_site_manifest:
        raise RuntimeError("release verification receipt is not derived from the managed wheel")
    verify_installed_site(
        site,
        site_manifest,
        frozen=frozen_directories_required(build_info),
    )

    latest_state = bounded_bytes(STATE, "managed state", MAX_MANIFEST_BYTES)
    if latest_state != raw_state:
        try:
            retry = int(os.environ.get("T1000_BOOTSTRAP_STATE_RETRY", "0"))
        except ValueError:
            retry = 0
        if retry >= 3:
            raise RuntimeError("managed state changed repeatedly during launch")
        os.environ["T1000_BOOTSTRAP_STATE_RETRY"] = str(retry + 1)
        os.execv(
            str(configured_python),
            [
                str(configured_python),
                "-I",
                "-B",
                str(bootstrap),
                *sys.argv[1:],
            ],
        )
    (
        import_snapshot,
        import_snapshot_descriptor,
        import_root,
        import_snapshot_files,
        import_snapshot_directories,
        import_snapshot_identities,
    ) = materialize_import_snapshot(
        ROOT,
        release,
        site_manifest,
        pinned_members,
    )
    atexit.register(
        cleanup_import_snapshot,
        import_snapshot,
        import_snapshot_descriptor,
        import_snapshot_files,
        import_snapshot_directories,
        import_snapshot_identities,
    )
except Exception as exc:
    print(f"t1000 managed launcher refused an invalid release: {exc}", file=sys.stderr)
    raise SystemExit(78)

os.environ.pop("T1000_BOOTSTRAP_STATE_RETRY", None)
os.environ["T1000_MANAGED_ROOT"] = str(ROOT)
os.environ["T1000_MANAGED_BIN_DIR"] = str(launcher.parent)
os.environ["T1000_RELEASE_MANIFEST"] = str(manifest_path)
os.environ["T1000_RELEASE_STATE"] = str(STATE)
verified_snapshot_importer = VerifiedSnapshotImporter(import_root, pinned_members)
sys.meta_path.insert(0, verified_snapshot_importer)
sys.path.insert(0, str(import_root))
del pinned_members
try:
    from termdeck.storage_fabric import require_hot_storage
    require_hot_storage(
        managed_root=ROOT,
        launcher_path=launcher,
        production=True,
    )
except Exception as exc:
    print(f"t1000 managed launcher refused unsafe storage: {exc}", file=sys.stderr)
    raise SystemExit(78)
from termdeck.build_info import release_status
status = release_status(verify_artifact=False, managed_root=ROOT)
if not status.get("ok") or status.get("release_id") != release_id:
    print("t1000 managed launcher rejected inconsistent bundled identity", file=sys.stderr)
    raise SystemExit(78)
from termdeck.cli import main
sys.argv[0] = "t1000"
raise SystemExit(main())
