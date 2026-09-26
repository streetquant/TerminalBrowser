#!/usr/bin/env python3
"""Build the explicit preparer payload with native Arch tools, without root.

This builds an artifact, never installs it. See the install plan for scoped
provisioning, first-install preconditions and package rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import sys
import tarfile


class PackageError(ValueError):
    pass


DEPENDENCIES = ("bash", "coreutils", "gawk", "iproute2", "openssl", "python", "systemd")
HOOK_OVERRIDES = (
    "20-systemd-sysusers.hook",
    "21-systemd-tmpfiles.hook",
    "30-systemd-daemon-reload-system.hook",
    "35-systemd-enqueue-marked.hook",
    "35-systemd-update.hook",
)
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def _installer():
    path = Path(__file__).with_name("install-preparer-assets.py")
    spec = importlib.util.spec_from_file_location("termdeck_arch_payload", path)
    if spec is None or spec.loader is None:
        raise PackageError("payload validator unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _archive_bytes(package: Path) -> bytes:
    info = package.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PackageError("package must be one regular unlinked file")
    with subprocess.Popen(
        ["/usr/bin/zstd", "-dc", "--", str(package)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        content = process.stdout.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            process.kill()
            raise PackageError("package exceeds archive bound")
        if process.wait(timeout=30):
            raise PackageError("package decompression failed")
    return content


def verify_package(package: Path, entries: list[dict[str, str]]) -> dict[str, object]:
    expected = {entry["destination"].removeprefix("/"): entry for entry in entries}
    directories = {
        str(parent)
        for name in expected for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    metadata = {".PKGINFO", ".BUILDINFO", ".MTREE"}
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(_archive_bytes(package)), mode="r:") as archive:
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/")
                if name in seen or name not in expected.keys() | directories | metadata:
                    raise PackageError(f"unexpected or duplicate package member: {name}")
                seen.add(name)
                if member.uid != 0 or member.gid != 0:
                    raise PackageError(f"package owner is not root: {name}")
                if name in directories:
                    if not member.isdir() or member.mode != 0o755:
                        raise PackageError(f"unsafe package directory: {name}")
                    continue
                if not member.isreg():
                    raise PackageError(f"package member is not a regular file: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise PackageError(f"missing package member: {name}")
                content = stream.read()
                mode = int(expected[name]["mode"], 8) if name in expected else 0o644
                if member.mode != mode:
                    raise PackageError(f"package mode mismatch: {name}")
                if name in expected and hashlib.sha256(content).hexdigest() != expected[name]["sha256"]:
                    raise PackageError(f"package content mismatch: {name}")
                if name == ".PKGINFO":
                    fields: dict[str, list[str]] = {}
                    for line in content.decode("utf-8").splitlines():
                        if " = " in line and not line.startswith("#"):
                            key, value = line.split(" = ", 1)
                            fields.setdefault(key, []).append(value)
                    if fields.get("pkgname") != ["termdeck-preparer"]:
                        raise PackageError("unexpected package name")
                    if sorted(fields.get("depend", [])) != sorted(DEPENDENCIES):
                        raise PackageError("unexpected package dependencies")
                    if fields.get("arch") != ["any"]:
                        raise PackageError("unexpected package architecture")
        if seen != expected.keys() | directories | metadata:
            raise PackageError("package is missing required members")
    except (tarfile.TarError, UnicodeError, OSError) as exc:
        raise PackageError(f"invalid package: {exc}") from exc
    return {"payload_files": len(expected), "members": len(seen), "ok": True}


def build(source_root: Path, output: Path, epoch: int) -> dict[str, object]:
    if os.geteuid() == 0:
        raise PackageError("build as an ordinary user, never root")
    if type(epoch) is not int or not 1 <= epoch < 2**31:
        raise PackageError("epoch must be a positive reproducible Unix timestamp")
    source_root, output = source_root.absolute(), output.absolute()
    if output == Path("/") or output.is_symlink():
        raise PackageError("output must be a private, empty, real directory")
    try:
        info = output.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or any(output.iterdir())):
            raise PackageError("output must be a private, empty, owned directory (0700)")
        if output.resolve() != output:
            raise PackageError("output must not have symlink parents")
    except OSError as exc:
        raise PackageError(f"output unavailable: {exc}") from exc
    installer = _installer()
    try:
        entries = installer.load_manifest(source_root / "deploy/preparer-install-manifest.json")
        sources = installer.validate_sources(source_root, entries)
    except installer.InstallError as exc:
        raise PackageError(str(exc)) from exc
    records = [
        {"destination": entry.destination, "mode": entry.mode,
         "sha256": hashlib.sha256(source.content).hexdigest()}
        for entry, source in zip(entries, sources, strict=True)
    ]
    payload_digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    with tarfile.open(output / "payload.tar", "w", format=tarfile.USTAR_FORMAT) as archive:
        for entry, source in zip(entries, sources, strict=True):
            member = tarfile.TarInfo(entry.destination.removeprefix("/"))
            member.size = len(source.content)
            member.mode = int(entry.mode, 8)
            member.mtime = epoch
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            archive.addfile(member, io.BytesIO(source.content))
    tar_digest = hashlib.sha256((output / "payload.tar").read_bytes()).hexdigest()
    version = f"1.{epoch}.h{payload_digest[:12]}"
    package_commands = "\n".join(
        f'  install -Dm{entry.mode} "$srcdir{entry.destination}" "$pkgdir{entry.destination}"'
        for entry in entries
    )
    (output / "PKGBUILD").write_text(
        f"pkgname=termdeck-preparer\npkgver={version}\npkgrel=1\n"
        "pkgdesc='Qualified TermDeck runtime preparation helpers'\n"
        "arch=('any')\nlicense=('MIT')\n"
        f"depends=({' '.join(shlex.quote(item) for item in DEPENDENCIES)})\n"
        f"source=('payload.tar')\nsha256sums=('{tar_digest}')\n"
        "package() {\n" + package_commands + "\n}\n"
    )
    # Explicit configuration avoids both system and user makepkg overrides.
    # No root, downloads, dependency installation, scriptlets or service starts.
    (output / "makepkg.conf").write_text(
        "CARCH=x86_64\nCHOST=x86_64-pc-linux-gnu\n"
        "BUILDENV=(!distcc !color !ccache check !sign)\n"
        "OPTIONS=(!strip docs libtool staticlibs emptydirs !zipman !purge !debug !lto !autodeps)\n"
        "INTEGRITY_CHECK=(sha256)\nDLAGENTS=()\nVCSCLIENTS=()\n"
        "COMPRESSZST=(zstd -c -T1 -)\nPKGEXT='.pkg.tar.zst'\nSRCEXT='.src.tar.gz'\n"
        "PACKAGER='TermDeck local qualification <local@termdeck.invalid>'\n"
    )
    command = [
        "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--dir", "/var/lib",
        "--ro-bind", "/var/lib/pacman", "/var/lib/pacman",
        "--bind", str(output), "/build", "--chdir", "/build",
        "--", "/usr/bin/makepkg", "--config", "/build/makepkg.conf", "--noconfirm",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin", "LC_ALL": "C", "SOURCE_DATE_EPOCH": str(epoch)},
    )
    (output / "build.stdout").write_text(result.stdout)
    (output / "build.stderr").write_text(result.stderr)
    if result.returncode:
        raise PackageError(f"makepkg failed ({result.returncode}); see {output}/build.stderr")
    packages = list(output.glob("*.pkg.tar.zst"))
    if len(packages) != 1:
        raise PackageError("build did not produce exactly one package")
    package = packages[0]
    verification = verify_package(package, records)
    hooks = output / "pacman-hooks"
    hooks.mkdir(mode=0o700)
    for name in HOOK_OVERRIDES:
        (hooks / name).symlink_to("/dev/null")
    receipt = {
        "schema": "termdeck.preparer-arch-package.v1", "ok": True,
        "package": package.name, "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "payload_sha256": payload_digest, "entries": records, "epoch": epoch,
        "verification": verification, "hook_overrides": list(HOOK_OVERRIDES),
        "installed": False,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(f"{receipt['sha256']}  {package.name}\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True, help="existing empty mode0700 directory")
    parser.add_argument("--epoch", type=int, required=True, help="fixed SOURCE_DATE_EPOCH")
    arguments = parser.parse_args()
    try:
        receipt = build(arguments.source_root, arguments.output, arguments.epoch)
    except (PackageError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
