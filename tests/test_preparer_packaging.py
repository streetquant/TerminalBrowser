from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import textwrap

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 package dependency
    import tomli as tomllib

import pytest


ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts/deploy/install-preparer-assets.py"
MANIFEST = ROOT / "deploy/preparer-install-manifest.json"
SOURCE_MANIFEST = ROOT / "MANIFEST.in"


def _load_installer():
    spec = importlib.util.spec_from_file_location("termdeck_preparer_installer", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_sources(tmp_path: Path, installer) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    for index, entry in enumerate(installer.EXPECTED_ENTRIES):
        source = root / entry.source
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"fixture-{index}\n".encode())
    return root


def test_explicit_preparer_manifest_stages_only_allowlisted_package_files(tmp_path: Path) -> None:
    installer = _load_installer()
    entries = installer.load_manifest(MANIFEST)
    source_root = _fixture_sources(tmp_path, installer)
    sources = installer.validate_sources(source_root, entries)
    destdir = tmp_path / "pkg"
    destdir.mkdir()

    installer.stage(destdir, entries, sources)

    installed = sorted(path for path in destdir.rglob("*") if path.is_file())
    assert ["/" + str(path.relative_to(destdir)) for path in installed] == sorted(
        entry.destination for entry in entries
    )
    for entry, source in zip(entries, sources, strict=True):
        destination = destdir / entry.destination.removeprefix("/")
        assert destination.read_bytes() == source.content
        assert stat.S_IMODE(destination.stat().st_mode) == int(entry.mode, 8)


def test_preparer_manifest_refuses_unlisted_privileged_asset(tmp_path: Path) -> None:
    installer = _load_installer()
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    value["entries"].append(
        {
            "destination": "/usr/libexec/unreviewed",
            "group": "root",
            "mode": "0755",
            "owner": "root",
            "source": "scripts/deploy/unreviewed",
        }
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(installer.InstallError, match="explicit privileged allowlist"):
        installer.load_manifest(manifest)


def test_privileged_payload_is_explicit_in_sdist_and_absent_from_wheel_data() -> None:
    installer = _load_installer()
    source_members = {
        line.removeprefix("include ")
        for raw in SOURCE_MANIFEST.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()).startswith("include ")
    }
    expected_sources = {entry.source for entry in installer.EXPECTED_ENTRIES}
    assert source_members == expected_sources | {
        "deploy/preparer-install-manifest.json",
        "deploy/termdeck-preparer-install-plan.md",
        "scripts/deploy/install-preparer-assets.py",
    }

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]
    assert "data-files" not in setuptools
    package_patterns = {
        pattern
        for patterns in setuptools.get("package-data", {}).values()
        for pattern in patterns
    }
    assert not any(
        pattern.startswith(("deploy/", "scripts/", "../"))
        for pattern in package_patterns
    )


def test_repository_contains_complete_preparer_payload_and_exact_daemon_argv() -> None:
    installer = _load_installer()
    entries = installer.load_manifest(MANIFEST)
    assert len(installer.validate_sources(ROOT, entries)) == len(entries)

    unit = (ROOT / "deploy/systemd/termdeck-runtime-attestd.service").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "scripts/deploy/termdeck-runtime-attestd").read_text(
        encoding="utf-8"
    )
    assert "ExecStart=/usr/libexec/termdeck-runtime-attestd daemon" in unit
    assert "raise SystemExit(main())" in launcher
    assert 'main(("daemon"' not in launcher


@pytest.fixture
def staged_package(tmp_path: Path) -> Path:
    installer = _load_installer()
    entries = installer.load_manifest(MANIFEST)
    package = tmp_path / "package"
    package.mkdir()
    installer.stage(package, entries, installer.validate_sources(ROOT, entries))
    return package


def _sandbox(package: Path, *arguments: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Run actual installed paths in a disposable root namespace, without sudo.

    Only the host's read-only /usr and this test's staged package are mounted.
    The namespace maps the current user to root; /usr and /usr/lib themselves
    are new root-owned directories so the production ancestor checks run intact.
    """

    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("staged executable qualification requires bubblewrap")
    python_library = f"python{sys.version_info.major}.{sys.version_info.minor}"
    command = [
        bwrap, "--unshare-user", "--uid", "0", "--gid", "0",
        "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
        "--new-session", "--die-with-parent",
        "--ro-bind", "/usr", "/host-usr",
        "--dir", "/usr", "--symlink", "/host-usr/bin", "/usr/bin",
        "--dir", "/usr/lib",
        "--symlink", f"/host-usr/lib/{python_library}", f"/usr/lib/{python_library}",
        "--symlink", "/host-usr/lib", "/lib",
        "--symlink", "/host-usr/lib", "/lib64",
        "--symlink", "/usr/bin", "/bin",
        "--ro-bind", str(package / "usr/libexec"), "/usr/libexec",
        "--ro-bind", str(package / "usr/lib/termdeck-preparer"), "/usr/lib/termdeck-preparer",
        "--tmpfs", "/tmp", "--chdir", "/tmp", "--proc", "/proc", "--dev", "/dev",
        "--setenv", "PYTHONPATH", "/poison",
        "--setenv", "LD_LIBRARY_PATH", "/host-usr/lib",
    ]
    # OpenSSL deliberately clears its environment.  Supply its real system
    # dependencies at their normal names in this otherwise empty /usr/lib.
    libraries: set[str] = set()
    for executable in ("/usr/bin/python3", "/usr/bin/openssl"):
        linked = subprocess.run(["ldd", executable], capture_output=True, text=True, check=True)
        libraries.update(re.findall(r"=> (/usr/lib/\S+)", linked.stdout))
    for library in sorted(libraries):
        command.extend(("--symlink", "/host-usr/lib/" + Path(library).name, library))
    command.extend(extra)
    command.extend(("--", "/usr/bin/python3", "-I", *arguments))
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


@pytest.mark.parametrize("helper", ("termdeck-runtime-attestd", "termdeck-preparer-state"))
def test_staged_helpers_execute_without_checkout_or_pythonpath(
    staged_package: Path, tmp_path: Path, helper: str,
) -> None:
    poison = tmp_path / "poison"
    (poison / "termdeck").mkdir(parents=True)
    (poison / "termdeck/__init__.py").write_text("raise RuntimeError('external package executed')\n")

    result = _sandbox(
        staged_package, f"/usr/libexec/{helper}", "--help",
        extra=("--ro-bind", str(poison), "/poison"),
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "external package executed" not in result.stderr


@pytest.mark.parametrize("helper", ("termdeck-runtime-attestd", "termdeck-preparer-state"))
@pytest.mark.parametrize("damage", ("missing", "symlink", "symlink-directory", "hardlink", "writable-file", "writable-directory", "unsafe-ancestor", "wrong-owner"))
def test_staged_helpers_refuse_incomplete_or_unsafe_private_library(
    staged_package: Path, helper: str, damage: str,
) -> None:
    library = staged_package / "usr/lib/termdeck-preparer/termdeck"
    module = library / "runtime_attestation.py"
    extra: tuple[str, ...] = ()
    if damage == "missing":
        module.unlink()
    elif damage == "symlink":
        module.rename(library / "retained.py")
        module.symlink_to("retained.py")
    elif damage == "symlink-directory":
        library.rename(library.with_name("retained"))
        library.symlink_to("retained", target_is_directory=True)
    elif damage == "hardlink":
        os.link(module, library / "retained.py")
    elif damage == "writable-file":
        module.chmod(0o666)
    elif damage == "writable-directory":
        library.chmod(0o777)
    elif damage == "unsafe-ancestor":
        extra = ("--chmod", "0777", "/usr/lib")
    else:
        # Host root is unmapped in this user namespace; the mounted image is
        # therefore not owned by namespace root, independently of its bytes.
        if os.geteuid() == 0:
            pytest.skip("owner rejection needs an unprivileged user namespace mapping")
        system_module = f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/os.py"
        extra = ("--ro-bind", system_module, "/usr/lib/termdeck-preparer/termdeck/runtime_attestation.py")

    result = _sandbox(staged_package, f"/usr/libexec/{helper}", "--help", extra=extra)

    assert result.returncode != 0
    assert "TermDeck privileged helper refused:" in result.stderr
    assert "usage:" not in result.stdout


ROUNDTRIP = textwrap.dedent('''
    import json, os, runpy
    from pathlib import Path
    bootstrap = runpy.run_path('/usr/libexec/termdeck-preparer-state')
    modules = bootstrap['_load_private_package']()
    attest = modules['termdeck.runtime_attestation']
    state = modules['termdeck.preparer_state']
    base = Path('/tmp/disposable')
    base.mkdir(mode=0o700)
    control = base / 'control'
    control.mkdir(mode=0o710)
    control.chmod(0o710)
    records = control / 'records'
    records.mkdir(mode=0o700)
    runtime = base / 'runtime'
    runtime.mkdir(mode=0o700)
    config = attest.DaemonConfig(
        control_root=control, socket_path=control / 'runtime-attestd.sock',
        records_path=records, private_key_path=control / 'runtime-attestd.key',
        public_key_path=base / 'runtime-attestd.pub',
        runtime_receipt_path=runtime / 'build-receipt.json',
        preparer=attest.Principal('disposable', os.geteuid(), os.getegid()),
    )
    attest.generate_keypair(config)
    transaction = 'a' * 32
    digest = 'b' * 64
    outputs = {
        name: {'path': name, 'sha256': digest, 'mode': 0o600,
               'identity': [1, inode, 0o600, 1, 12, 20, 21]}
        for inode, name in enumerate(('current-lan-ip', 'nginx/allowed-hosts.map'), 100)
    }
    modes = {'ca.crt': 0o600, 'ca.key': 0o600, 'server-chain.crt': 0o644,
             'server.crt': 0o644, 'server.ext': 0o600, 'server.key': 0o600}
    core = {
        'files': {name: digest for name in (
            'bin/termdeck-web-server', 'nginx/nginx.conf', 'tls/ca.crt',
            'tls/server-chain.crt', 'tls/server.crt')},
        'https_port': 7443, 'lan_ip': '192.168.50.9', 'ok': True,
        'runtime_publication': {'generation': transaction, 'outputs': outputs},
        'schema': attest.REQUEST_SCHEMA,
        'tls_publication': {
            'generation': transaction,
            'current': {'path': 'tls', 'identity': [1, 300, 0o700, os.geteuid()],
                        'manifest': {
                            name: {'identity': [1, inode, mode, 1, 12, 20, 21],
                                   'mode': mode, 'sha256': digest}
                            for inode, (name, mode) in enumerate(modes.items(), 200)}},
            'prior': None,
        },
    }
    config.runtime_receipt_path.write_bytes(state._canonical_json(core))
    config.runtime_receipt_path.chmod(0o644)
    before = config.runtime_receipt_path.stat()
    daemon = attest.RuntimeAttestationDaemon(config)
    def signer(request, descriptor, supplied_config):
        assert supplied_config is config
        return daemon.attest(request, receipt_descriptor=descriptor,
            peer=attest.PeerCredentials(os.getpid(), os.geteuid(), os.getegid()))
    receipt = state.finalize_runtime_receipt(config.runtime_receipt_path,
        transaction, config=config, signer=signer)
    attest.verify_runtime_build_attestation(receipt, config=config)
    after = config.runtime_receipt_path.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    try:
        attest.generate_keypair(config)
    except attest.AttestationError:
        pass
    else:
        raise AssertionError('existing signer pair was replaced')
    print(json.dumps({'ok': True, 'algorithm': receipt['producer_attestation']['attestation']['algorithm'],
                      'modules': {name: module.__file__ for name, module in modules.items()}}))
''')


def test_staged_private_package_signs_verifies_and_recovers_after_complete_restore(staged_package: Path) -> None:
    library = staged_package / "usr/lib/termdeck-preparer/termdeck"
    module = library / "runtime_attestation.py"
    preimages = {path: path.read_bytes() for path in staged_package.rglob("*") if path.is_file()}

    first = _sandbox(staged_package, "-c", ROUNDTRIP)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["ok"] is True
    assert report["algorithm"] == "ed25519-openssl-pkeyutl.v1"
    assert set(report["modules"].values()) == {
        "/usr/lib/termdeck-preparer/termdeck/" + leaf
        for leaf in ("__init__.py", "runtime_attestation.py", "preparer_state.py")
    }

    module.unlink()
    broken = _sandbox(staged_package, "/usr/libexec/termdeck-preparer-state", "--help")
    assert broken.returncode != 0
    module.write_bytes(preimages[module])
    module.chmod(0o644)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in preimages} == {
        path: hashlib.sha256(content).hexdigest() for path, content in preimages.items()
    }
    restored = _sandbox(staged_package, "-c", ROUNDTRIP)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["ok"] is True
