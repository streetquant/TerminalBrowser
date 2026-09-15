from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys

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
