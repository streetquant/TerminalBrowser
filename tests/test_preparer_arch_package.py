from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/deploy/build-preparer-arch-package.py"
MANIFEST = ROOT / "deploy/preparer-install-manifest.json"
EPOCH = 1790377200
HOOKS = (
    "20-systemd-sysusers.hook",
    "21-systemd-tmpfiles.hook",
    "30-systemd-daemon-reload-system.hook",
    "35-systemd-enqueue-marked.hook",
    "35-systemd-update.hook",
)


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("termdeck_preparer_arch_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _private_output(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


@pytest.fixture(scope="module")
def built_packages(builder, tmp_path_factory):
    try:
        distribution = platform.freedesktop_os_release()
    except OSError:
        distribution = {}
    families = {distribution.get("ID", ""), *distribution.get("ID_LIKE", "").split()}
    native_database = Path("/var/lib/pacman/local").is_dir()
    if distribution.get("ID") and "arch" not in families and not native_database:
        pytest.skip("Native Arch package integration does not apply to this non-Arch host without a pacman database")
    assert native_database, "Arch package qualification requires the native pacman database"
    prerequisites = ("bwrap", "makepkg", "fakeroot", "pacman", "zstd", "bsdtar")
    missing = [name for name in prerequisites if shutil.which(name) is None]
    assert not missing, f"Arch package qualification prerequisites missing: {missing}"
    assert os.geteuid() != 0, "Run package qualification as an ordinary user, never host root"
    first = _private_output(tmp_path_factory.mktemp("arch-first") / "output")
    second = _private_output(tmp_path_factory.mktemp("arch-second") / "other-output")
    return (
        (first, builder.build(ROOT, first, EPOCH)),
        (second, builder.build(ROOT, second, EPOCH)),
    )


def _tar_bytes(package: Path) -> bytes:
    result = subprocess.run(
        ["zstd", "--quiet", "--decompress", "--stdout", "--", str(package)],
        capture_output=True, check=True, timeout=30,
    )
    return result.stdout


def test_native_package_is_reproducible_and_contains_exact_manifest(built_packages, builder):
    (first, receipt), (second, repeated) = built_packages
    package = first / receipt["package"]
    other = second / repeated["package"]
    assert package.read_bytes() == other.read_bytes()
    assert receipt["sha256"] == repeated["sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert receipt["payload_sha256"] == repeated["payload_sha256"]
    expected = json.loads(MANIFEST.read_text())["entries"]
    records = [
        {"destination": row["destination"], "mode": row["mode"],
         "sha256": hashlib.sha256((ROOT / row["source"]).read_bytes()).hexdigest()}
        for row in expected
    ]
    assert receipt["entries"] == records
    assert builder.verify_package(package, records)["ok"] is True
    assert receipt["installed"] is False
    assert json.loads((first / "receipt.json").read_text()) == receipt
    assert tuple(receipt["hook_overrides"]) == HOOKS
    assert sorted(path.name for path in (first / "pacman-hooks").iterdir()) == sorted(HOOKS)
    for name in HOOKS:
        hook = first / "pacman-hooks" / name
        assert hook.is_symlink() and os.readlink(hook) == "/dev/null"

    payload = {row["destination"].lstrip("/"): row for row in expected}
    directories = {str(parent) for name in payload for parent in Path(name).parents if str(parent) != "."}
    with tarfile.open(fileobj=io.BytesIO(_tar_bytes(package))) as archive:
        members = archive.getmembers()
        names = [member.name.removeprefix("./").rstrip("/") for member in members]
        assert len(names) == len(set(names))
        assert set(names) == set(payload) | directories | {".PKGINFO", ".BUILDINFO", ".MTREE"}
        for member, name in zip(members, names, strict=True):
            assert (member.uid, member.gid) == (0, 0)
            if name in directories:
                assert member.isdir() and member.mode == 0o755
                continue
            assert member.isreg()
            assert member.mode == (int(payload[name]["mode"], 8) if name in payload else 0o644)
            if name in payload:
                assert archive.extractfile(member).read() == (ROOT / payload[name]["source"]).read_bytes()


@pytest.mark.parametrize("damage", ("missing", "nonempty", "public", "symlink", "linked-parent"))
def test_builder_refuses_unsafe_output_before_writing(builder, tmp_path, damage):
    output = tmp_path / "output"
    if damage != "missing":
        output.mkdir(mode=0o700)
    sentinel = tmp_path / "preserve"
    sentinel.write_bytes(b"unrelated preimage\n")
    if damage == "nonempty":
        (output / "existing").write_bytes(b"retain\n")
    elif damage == "public":
        output.chmod(0o755)
    elif damage == "symlink":
        output.rmdir()
        output.symlink_to(tmp_path)
    elif damage == "linked-parent":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / "output"
    with pytest.raises(builder.PackageError):
        builder.build(ROOT, output, EPOCH)
    assert sentinel.read_bytes() == b"unrelated preimage\n"
    assert not list(tmp_path.rglob("*.pkg.tar.zst"))


def _source_copy(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    paths = {row["source"] for row in json.loads(MANIFEST.read_text())["entries"]}
    paths.add("deploy/preparer-install-manifest.json")
    for relative in paths:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return root


@pytest.mark.parametrize("damage", ("extra-manifest-entry", "manifest-link", "source-link", "source-hardlink"))
def test_builder_refuses_manifest_or_source_substitution(builder, tmp_path, damage):
    source = _source_copy(tmp_path)
    manifest = source / "deploy/preparer-install-manifest.json"
    entry = source / "deploy/termdeck-preparer-broker"
    if damage == "extra-manifest-entry":
        value = json.loads(manifest.read_text())
        value["entries"].append(dict(value["entries"][0], destination="/usr/libexec/unreviewed"))
        manifest.write_text(json.dumps(value))
    elif damage == "manifest-link":
        saved = manifest.with_name("saved.json")
        manifest.rename(saved)
        manifest.symlink_to(saved.name)
    elif damage == "source-link":
        saved = entry.with_name("saved-source")
        entry.rename(saved)
        entry.symlink_to(saved.name)
    else:
        os.link(entry, entry.with_name("saved-source"))
    output = _private_output(tmp_path / "output")
    with pytest.raises(builder.PackageError):
        builder.build(source, output, EPOCH)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("damage", ("bytes", "mode", "owner", "symlink", "duplicate", "missing", "install-script", "unsafe-directory"))
def test_verifier_rejects_modified_archive(built_packages, builder, tmp_path, damage):
    (output, receipt), _ = built_packages
    source = output / receipt["package"]
    target = receipt["entries"][0]["destination"].lstrip("/")
    altered = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(_tar_bytes(source))) as archive, tarfile.open(fileobj=altered, mode="w") as result:
        for original in archive:
            member = copy.copy(original)
            name = member.name.removeprefix("./").rstrip("/")
            content = archive.extractfile(original).read() if original.isreg() else None
            if name == target:
                if damage == "missing":
                    continue
                if damage == "bytes":
                    content += b"modified\n"
                    member.size = len(content)
                elif damage == "mode":
                    member.mode = 0o777
                elif damage == "owner":
                    member.uid = 1000
                elif damage == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/tmp/substitute"
                    member.size = 0
                    content = None
            elif name == "usr/lib/termdeck-preparer" and damage == "unsafe-directory":
                member.mode = 0o777
            result.addfile(member, io.BytesIO(content) if content is not None else None)
            if name == target and damage == "duplicate":
                result.addfile(member, io.BytesIO(content))
        if damage == "install-script":
            content = b"post_install() { exit 1; }\n"
            member = tarfile.TarInfo(".INSTALL")
            member.mode = 0o644
            member.size = len(content)
            result.addfile(member, io.BytesIO(content))
    package = tmp_path / "altered.pkg.tar.zst"
    compressed = subprocess.run(["zstd", "--quiet", "--stdout"], input=altered.getvalue(), capture_output=True, check=True, timeout=30)
    package.write_bytes(compressed.stdout)
    with pytest.raises(builder.PackageError):
        builder.verify_package(package, receipt["entries"])


TRANSACTION_PROBE = textwrap.dedent(r'''
    import hashlib, json, os, re, subprocess, sys
    from pathlib import Path
    os.umask(0o022)
    package = Path('/packet') / sys.argv[1]
    entries = json.loads(sys.argv[2])
    hooks = json.loads(sys.argv[3])
    config = Path('/tmp/pacman.conf')
    config.write_text('[options]\nArchitecture = auto\nSigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\n')
    Path('/tmp/cache').mkdir()
    Path('/tmp/gnupg').mkdir(mode=0o700)
    instrumented = Path('/tmp/instrumented')
    instrumented.mkdir()
    for name in hooks:
        content = (Path('/usr/share/libalpm/hooks') / name).read_text()
        content = re.sub(r'^Exec = .*$', 'Exec = /fixture-hook ' + name, content, flags=re.M)
        (instrumented / name).write_text(content)
    (instrumented / '99-independent-observer.hook').write_text(
        '[Trigger]\nType = Package\nOperation = Install\nOperation = Upgrade\nOperation = Remove\n'
        'Target = termdeck-preparer\n[Action]\nWhen = PostTransaction\n'
        'Exec = /fixture-hook independent-observer\n')
    linked = subprocess.check_output(['/usr/bin/ldd', '/usr/bin/bash'], text=True)
    libraries = set(re.findall(r'/(?:[^\s()]+)', linked))

    def prepare(name):
        root = Path('/tmp') / name
        root.mkdir()
        for path in ('usr/bin', 'usr/lib', 'usr/lib/systemd/system', 'var/lib/pacman', 'var/log', 'etc', 'tmp'):
            (root / path).mkdir(parents=True, exist_ok=True)
        (root / 'usr/bin/sh').write_bytes(Path('/usr/bin/bash').read_bytes())
        (root / 'usr/bin/sh').chmod(0o755)
        for path, target in (('bin', 'usr/bin'), ('lib', 'usr/lib'), ('lib64', 'usr/lib')):
            (root / path).symlink_to(target)
        for library in libraries:
            source = Path(library)
            if source.is_file():
                destination = root / source.relative_to('/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
                destination.chmod(0o755)
        (root / 'fixture-hook').write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> /hook-events\n')
        (root / 'fixture-hook').chmod(0o755)
        protected = (
            'warp/projects/softwares/termdeck/.termdeck/state/sessions.json',
            'var/lib/termdeck-preparer/runtime/transaction-journal.json',
            'etc/termdeck/control/runtime-attestd.key',
            'etc/passwd',
            'usr/libexec/unrelated-helper',
        )
        for index, name in enumerate(protected):
            leaf = root / name
            leaf.parent.mkdir(parents=True, exist_ok=True)
            leaf.write_bytes(('disposable fixture %s\n' % index).encode())
            leaf.chmod(0o600)
        return root, protected

    def snapshot(root, names):
        result = {}
        for name in names:
            path = root / name
            st = path.lstat()
            result[name] = (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, hashlib.sha256(path.read_bytes()).hexdigest())
        return result

    def pacman(root, masked, arguments, expected=0):
        # Dependencies are intentionally skipped ONLY in this isolated content
        # transaction fixture. Host dependencies and service runtime are not proved.
        command = ['/usr/bin/pacman', '--config', str(config), '--root', str(root),
            '--dbpath', str(root / 'var/lib/pacman'), '--logfile', str(root / 'var/log/pacman.log'),
            '--cachedir', '/tmp/cache', '--gpgdir', '/tmp/gnupg',
            '--hookdir', str(instrumented), '--noconfirm']
        if masked:
            command.extend(('--hookdir', '/packet/pacman-hooks'))
        if arguments[0] in ('-U', '-R'):
            command.extend(('--nodeps', '--nodeps'))
        result = subprocess.run(command + arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if expected == 0:
            assert result.returncode == 0, result.stdout
        else:
            assert result.returncode != 0, result.stdout
        return result.stdout

    def verify_payload(root):
        for entry in entries:
            path = root / entry['destination'].lstrip('/')
            st = path.lstat()
            assert path.is_file() and not path.is_symlink()
            assert (st.st_uid, st.st_gid) == (0, 0)
            assert st.st_mode & 0o7777 == int(entry['mode'], 8)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']

    for label, masked in (('baseline', False), ('masked', True)):
        root, protected = prepare(label)
        prior = snapshot(root, protected)
        pacman(root, masked, ['-U', str(package)])
        verify_payload(root)
        first = (root / 'hook-events').read_text().splitlines()
        expected = ['independent-observer'] if masked else [name for name in hooks if name != '35-systemd-enqueue-marked.hook'] + ['independent-observer']
        assert first == expected, (label, first)
        pacman(root, masked, ['-U', str(package)])
        verify_payload(root)
        replay = (root / 'hook-events').read_text().splitlines()[len(first):]
        assert replay == (['independent-observer'] if masked else hooks + ['independent-observer']), replay
        pacman(root, masked, ['-Q', 'termdeck-preparer'])
        assert snapshot(root, protected) == prior
        pacman(root, masked, ['-R', 'termdeck-preparer'])
        assert snapshot(root, protected) == prior
        assert all(not (root / entry['destination'].lstrip('/')).exists() for entry in entries)
        pacman(root, masked, ['-Q', 'termdeck-preparer'], expected=1)
        removed = (root / 'hook-events').read_text().splitlines()[len(first) + len(replay):]
        assert removed == (['independent-observer'] if masked else ['30-systemd-daemon-reload-system.hook', '35-systemd-update.hook', 'independent-observer']), removed
        print(json.dumps({'case': label, 'install_hooks': first, 'replay_hooks': replay, 'remove_hooks': removed, 'protected_preimages': True}))

    root, protected = prepare('conflict')
    conflict = root / entries[0]['destination'].lstrip('/')
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b'pre-existing unowned asset\n')
    protected = (*protected, str(conflict.relative_to(root)))
    prior = snapshot(root, protected)
    output = pacman(root, True, ['-U', str(package)], expected=1)
    assert 'exists in filesystem' in output, output
    assert snapshot(root, protected) == prior
    assert not (root / 'hook-events').exists()
    assert all(not (root / entry['destination'].lstrip('/')).exists() for entry in entries[1:])
    pacman(root, True, ['-Q', 'termdeck-preparer'], expected=1)
    print(json.dumps({'case': 'conflict', 'refused': True, 'protected_preimages': True, 'dependencies_checked': False}))
''')


def _run_transaction_probe(output: Path, receipt: dict) -> list[dict]:
    command = [
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--uid", "0", "--gid", "0",
        "--cap-add", "CAP_SYS_CHROOT", "--cap-add", "CAP_CHOWN", "--cap-add", "CAP_DAC_OVERRIDE",
        "--clearenv", "--setenv", "PATH", "/usr/bin", "--setenv", "LC_ALL", "C",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--tmpfs", "/etc", "--tmpfs", "/var", "--ro-bind", str(output), "/packet",
        "--chdir", "/tmp", "/usr/bin/python3", "-I", "-c", TRANSACTION_PROBE,
        receipt["package"], json.dumps(receipt["entries"]), json.dumps(list(HOOKS)),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_real_pacman_install_replay_conflict_and_removal_are_confined(built_packages):
    (output, receipt), _ = built_packages
    observations = _run_transaction_probe(output, receipt)
    assert [row["case"] for row in observations] == ["baseline", "masked", "conflict"]
    assert all(row["protected_preimages"] for row in observations)
