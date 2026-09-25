#!/usr/bin/python3 -I
"""Validate or stage TermDeck's explicit privileged package payload."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Final, Mapping, Sequence


SCHEMA: Final = "termdeck.preparer-install-manifest.v1"
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024


class InstallError(RuntimeError):
    """The package payload is incomplete or violates its fixed layout."""


@dataclass(frozen=True, order=True)
class Entry:
    source: str
    destination: str
    mode: str
    owner: str
    group: str


@dataclass(frozen=True)
class SourceArtifact:
    path: Path
    content: bytes


# Privileged package files are deliberately enumerated here as well as in the
# manifest.  Adding an unrelated file below deploy/ or scripts/deploy/ cannot
# silently turn it into a root-owned installed asset.
EXPECTED_ENTRIES: Final = (
    Entry(
        "scripts/deploy/termdeck-runtime-attestd",
        "/usr/libexec/termdeck-runtime-attestd",
        "0755",
        "root",
        "root",
    ),
    Entry(
        "deploy/termdeck-preparer-broker",
        "/usr/libexec/termdeck-preparer-broker",
        "0755",
        "root",
        "root",
    ),
    Entry(
        "deploy/termdeck-preparer-publication",
        '/usr/libexec/termdeck-preparer-publication',
        "0755",
        "root",
        "root",
    ),
    Entry(
        "scripts/termdeck-preparer-state.py",
        "/usr/libexec/termdeck-preparer-state",
        "0755",
        "root",
        "root",
    ),
    Entry(
        "scripts/termdeck-web-prepare-runtime.sh",
        "/usr/libexec/termdeck-web-prepare-runtime",
        "0755",
        "root",
        "root",
    ),
    Entry(
        "deploy/systemd/termdeck-runtime-attestd.service",
        "/usr/lib/systemd/system/termdeck-runtime-attestd.service",
        "0644",
        "root",
        "root",
    ),
    Entry(
        "deploy/systemd/termdeck-preparer-broker.socket",
        "/usr/lib/systemd/system/termdeck-preparer-broker.socket",
        "0644",
        "root",
        "root",
    ),
    Entry(
        "deploy/systemd/termdeck-preparer-broker.service",
        "/usr/lib/systemd/system/termdeck-preparer-broker.service",
        "0644",
        "root",
        "root",
    ),
    Entry(
        "deploy/systemd/termdeck-preparer.sysusers.conf",
        "/usr/lib/sysusers.d/termdeck-preparer.conf",
        "0644",
        "root",
        "root",
    ),
    Entry(
        "deploy/systemd/termdeck-preparer.tmpfiles.conf",
        "/usr/lib/tmpfiles.d/termdeck-preparer.conf",
        "0644",
        "root",
        "root",
    ),
)


def _manifest_object(path: Path) -> Mapping[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise InstallError("install manifest contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise InstallError("install manifest contains a non-finite JSON value")

    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallError("install manifest must be one regular, unlinked file")
        if info.st_size <= 0 or info.st_size > MAX_MANIFEST_BYTES:
            raise InstallError("install manifest size is unsafe")
        with path.open("rb") as stream:
            content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) != info.st_size:
            raise InstallError("install manifest changed while being read")
        value = json.loads(
            content,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not read install manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError("install manifest must be a JSON object")
    return value


def load_manifest(path: Path) -> tuple[Entry, ...]:
    value = _manifest_object(path)
    if set(value) != {"entries", "schema"} or value.get("schema") != SCHEMA:
        raise InstallError("install manifest schema is invalid")
    rows = value.get("entries")
    if not isinstance(rows, list):
        raise InstallError("install manifest entries must be a list")
    entries: list[Entry] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "destination",
            "group",
            "mode",
            "owner",
            "source",
        }:
            raise InstallError("install manifest entry shape is invalid")
        if not all(isinstance(row[key], str) for key in row):
            raise InstallError("install manifest entry values must be strings")
        entry = Entry(
            source=row["source"],
            destination=row["destination"],
            mode=row["mode"],
            owner=row["owner"],
            group=row["group"],
        )
        entries.append(entry)
    if tuple(entries) != EXPECTED_ENTRIES:
        raise InstallError("install manifest differs from the explicit privileged allowlist")
    return tuple(entries)


def _source_artifact(root: Path, entry: Entry) -> SourceArtifact:
    relative = PurePosixPath(entry.source)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise InstallError(f"unsafe package source path: {entry.source}")
    source = root.joinpath(*relative.parts)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise InstallError(f"package source is unavailable: {entry.source}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise InstallError(f"package source is not one regular, unlinked file: {entry.source}")
        if before.st_size <= 0 or before.st_size > MAX_SOURCE_BYTES:
            raise InstallError(f"package source size is unsafe: {entry.source}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise InstallError(f"package source changed while being read: {entry.source}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise InstallError(f"package source grew while being read: {entry.source}")
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != before_identity:
            raise InstallError(f"package source changed while being read: {entry.source}")
        return SourceArtifact(source, b"".join(chunks))
    finally:
        os.close(descriptor)


def validate_sources(root: Path, entries: Sequence[Entry]) -> tuple[SourceArtifact, ...]:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise InstallError(f"source root is unavailable: {exc}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise InstallError("source root must be a real directory")
    return tuple(_source_artifact(root, entry) for entry in entries)


def _destination_path(destdir: Path, entry: Entry) -> Path:
    relative = PurePosixPath(entry.destination.removeprefix("/"))
    if (
        not entry.destination.startswith("/")
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise InstallError(f"unsafe package destination path: {entry.destination}")
    return destdir.joinpath(*relative.parts)


def _assert_real_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError(f"{label} is unavailable: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"{label} must be a real directory")


def _ensure_parent(destdir: Path, destination: Path) -> None:
    relative = destination.relative_to(destdir)
    current = destdir
    for component in relative.parts[:-1]:
        current = current / component
        try:
            current.mkdir(mode=0o755)
        except FileExistsError:
            pass
        _assert_real_directory(current, label="package destination parent")


def stage(
    destdir: Path,
    entries: Sequence[Entry],
    sources: Sequence[SourceArtifact],
) -> None:
    if not destdir.is_absolute() or destdir == Path("/"):
        raise InstallError("DESTDIR must be an absolute, non-root staging directory")
    _assert_real_directory(destdir, label="DESTDIR")
    destinations = tuple(_destination_path(destdir, entry) for entry in entries)
    for destination in destinations:
        if os.path.lexists(destination):
            raise InstallError(f"package destination already exists: {destination}")
    published: list[Path] = []
    try:
        for entry, source, destination in zip(entries, sources, destinations, strict=True):
            _ensure_parent(destdir, destination)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(destination, flags, int(entry.mode, 8))
            published.append(destination)
            try:
                view = memoryview(source.content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise InstallError("package destination write made no progress")
                    view = view[written:]
                os.fchmod(descriptor, int(entry.mode, 8))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        for destination in reversed(published):
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="validate or stage the explicit TermDeck preparer package payload"
    )
    parser.add_argument("--source-root", type=Path, default=repository)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository / "deploy/preparer-install-manifest.json",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--destdir", type=Path)
    arguments = parser.parse_args(argv)
    try:
        entries = load_manifest(arguments.manifest)
        sources = validate_sources(arguments.source_root.absolute(), entries)
        if arguments.destdir is not None:
            stage(arguments.destdir.absolute(), entries, sources)
    except InstallError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
