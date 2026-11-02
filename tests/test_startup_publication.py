"""Separate recovery publication must not modify current runtime/user data."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from termdeck import startup_publication as publication


@pytest.fixture
def layout(tmp_path):
    source = tmp_path / "source"
    package = source / "termdeck"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "startup_recovery.py").write_text("def main(): return 0\n")
    managed, state, units, evidence = (tmp_path / name for name in ("managed", "state", "units", "evidence"))
    for path in (managed, state, units, evidence):
        path.mkdir(mode=0o700)
    return source, managed, state, units, evidence


def bundle(layout):
    return Path(publication.prepare(layout[0], layout[1])["bundle"])


def test_immutable_bundle_is_verified_before_any_worker_import(layout):
    prepared = bundle(layout)
    result = subprocess.run(("/usr/bin/python3", "-I", "-B", str(prepared / "launch.py"),
                             "--verify-only"), capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    assert prepared.stat().st_mode & 0o222 == 0
    assert publication.prepare(layout[0], layout[1])["bundle"] == str(prepared)


@pytest.mark.parametrize("failure", ("bytes", "mode", "hardlink", "symlink", "extra"))
def test_payload_drift_fails_closed(layout, failure):
    prepared = bundle(layout)
    package = prepared / "site" / "termdeck"
    target = package / "startup_recovery.py"
    package.chmod(0o755)
    if failure == "bytes":
        target.chmod(0o644)
        target.write_text("def main(): return 1\n")
        target.chmod(0o444)
    elif failure == "mode":
        target.chmod(0o644)
    elif failure == "hardlink":
        (layout[4] / "foreign.py").hardlink_to(target)
    elif failure == "symlink":
        target.unlink()
        target.symlink_to(layout[0] / "termdeck" / "startup_recovery.py")
    else:
        (package / "extra.py").write_text("pass\n")
    with pytest.raises((publication.StartupPublicationError, OSError)):
        publication.verify_bundle(prepared)


def test_source_symlink_is_rejected(layout):
    linked = layout[4] / "linked"
    linked.symlink_to(layout[0], target_is_directory=True)
    with pytest.raises(publication.StartupPublicationError):
        publication.prepare(linked, layout[1])


@pytest.mark.parametrize("content", (b"{", b"[]", b"\xff"))
def test_malformed_manifest_is_rejected_without_publication(layout, content):
    prepared = bundle(layout)
    target = prepared / "manifest.json"
    target.chmod(0o644)
    target.write_bytes(content)
    target.chmod(0o444)
    with pytest.raises(publication.StartupPublicationError):
        publication.publish(prepared, *layout[1:4], layout[4] / "backup.json")
    assert not (layout[3] / publication.SERVICE).exists()


def test_source_changes_during_staging_cannot_publish(layout, monkeypatch):
    original = publication._source_files
    calls = 0
    def changing(source):
        nonlocal calls
        calls += 1
        if calls == 2:
            (layout[0] / "termdeck" / "startup_recovery.py").write_text("def main(): return 3\n")
        return original(source)
    monkeypatch.setattr(publication, "_source_files", changing)
    with pytest.raises(publication.StartupPublicationError, match="source changed"):
        publication.prepare(layout[0], layout[1])
    releases = layout[1] / "startup-recovery" / "releases"
    assert not [p for p in releases.iterdir() if not p.name.startswith(".stage-")]


def test_publication_and_inverse_preserve_unrelated_files(layout):
    prepared = bundle(layout)
    backend = layout[3] / "termdeck-web-backend.service"
    backend.write_bytes(b"unchanged current web service\n")
    history = layout[2] / "history"
    history.write_bytes(b"user history\n")
    backup = layout[4] / "backup.json"
    result = publication.publish(prepared, *layout[1:4], backup)
    assert result["state"] == "published"
    unit = (layout[3] / publication.SERVICE).read_text()
    assert "WantedBy=termdeck-web-backend.service" in unit
    assert "PartOf=termdeck-web-backend.service" in unit
    assert "RemainAfterExit=yes" in unit
    assert "restart" not in unit
    assert all(selector in unit for selector in publication.SELECTORS)
    owner = publication.pwd.getpwuid(os.geteuid())
    assert f'Environment=HOME="{owner.pw_dir}"' in unit
    assert f'Environment=PATH="{owner.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin"' in unit
    assert backend.read_bytes() == b"unchanged current web service\n"
    assert publication.rollback(backup, layout[1], layout[3])["state"] == "rolled-back"
    assert not (layout[3] / publication.SERVICE).exists()
    assert not (layout[1] / "startup-recovery" / "selection.json").exists()
    assert history.read_bytes() == b"user history\n"
    assert backend.read_bytes() == b"unchanged current web service\n"


def test_inverse_does_not_overwrite_later_unit_changes(layout):
    backup = layout[4] / "backup.json"
    publication.publish(bundle(layout), *layout[1:4], backup)
    unit = layout[3] / publication.SERVICE
    unit.write_bytes(unit.read_bytes() + b"# later user edit\n")
    changed = unit.read_bytes()
    with pytest.raises(publication.StartupPublicationError, match="changed after publication"):
        publication.rollback(backup, layout[1], layout[3])
    assert unit.read_bytes() == changed


def test_foreign_preexisting_service_is_not_replaced(layout):
    unit = layout[3] / publication.SERVICE
    unit.write_bytes(b"foreign user service\n")
    with pytest.raises(publication.StartupPublicationError, match="partial or foreign"):
        publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json")
    assert unit.read_bytes() == b"foreign user service\n"
    assert not (layout[4] / "backup.json").exists()


def test_overwrite_rollback_restores_exact_predecessor(layout):
    first = bundle(layout)
    initial = layout[4] / "initial.json"
    publication.publish(first, *layout[1:4], initial)
    unit = layout[3] / publication.SERVICE
    before = unit.read_bytes()
    (layout[0] / "termdeck" / "startup_recovery.py").write_text("def main(): return 2\n")
    next_bundle = bundle(layout)
    inverse = layout[4] / "inverse.json"
    publication.publish(next_bundle, *layout[1:4], inverse)
    assert unit.read_bytes() != before
    publication.rollback(inverse, layout[1], layout[3])
    assert unit.read_bytes() == before


def test_failed_verification_rolls_back_atomic_publication(layout, monkeypatch):
    prepared = bundle(layout)
    verify = publication.verify_bundle
    calls = 0
    def reject(bundle_path, entries=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise publication.StartupPublicationError("fixture failure after public rename")
        return verify(bundle_path, entries)
    monkeypatch.setattr(publication, "verify_bundle", reject)
    with pytest.raises(Exception, match="fixture failure"):
        publication.publish(prepared, *layout[1:4], layout[4] / "backup.json")
    assert not (layout[3] / publication.SERVICE).exists()
    assert not (layout[1] / "startup-recovery" / "selection.json").exists()


def test_activation_uses_only_owning_user_manager_never_web_restart(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json")
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        output = str(layout[3] / publication.SERVICE).encode() if "show" in argv else b""
        return subprocess.CompletedProcess(argv, 0, output, b"")
    publication.activate(layout[1], layout[3], run=run)
    assert seen == [("/usr/bin/systemctl", "--user", "daemon-reload"),
                    ("/usr/bin/systemctl", "--user", "show", publication.SERVICE, "-p", "FragmentPath", "--value"),
                    ("/usr/bin/systemctl", "--user", "enable", "--now", publication.SERVICE),
                    ("/usr/bin/systemctl", "--user", "is-enabled", publication.SERVICE),
                    ("/usr/bin/systemctl", "--user", "is-active", publication.SERVICE)]


def test_activation_failure_is_not_reported_active(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json")
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"fixture manager failure")
    with pytest.raises(publication.StartupPublicationError, match="activation failed"):
        publication.activate(layout[1], layout[3], run=run)


def test_explicit_deferred_account_is_bound_to_unit_and_selection(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json",
                        deferred_native_accounts=("fixture-account",))
    unit = (layout[3] / publication.SERVICE).read_text()
    assert '"--defer-native-account" "fixture-account"' in unit
    selected = publication._object((layout[1] / "startup-recovery" / "selection.json").read_bytes())
    assert selected["deferred_native_accounts"] == ["fixture-account"]
    def run(argv, **kwargs):
        value = str(layout[3] / publication.SERVICE).encode() if "show" in argv else b""
        return subprocess.CompletedProcess(argv, 0, value, b"")
    publication.activate(layout[1], layout[3], run=run)


@pytest.mark.parametrize("accounts", ("fixture-account", (None,), ("",), ("bad\naccount",)))
def test_invalid_deferred_accounts_cannot_publish(layout, accounts):
    with pytest.raises(publication.StartupPublicationError):
        publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json",
                            deferred_native_accounts=accounts)
    assert not (layout[3] / publication.SERVICE).exists()


def test_changed_deferral_cannot_activate_existing_unit(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json",
                        deferred_native_accounts=("fixture-account",))
    path = layout[1] / "startup-recovery" / "selection.json"
    selected = publication._object(path.read_bytes())
    selected["deferred_native_accounts"] = []
    path.write_bytes(publication._json(selected))
    def run(argv, **kwargs):
        pytest.fail("changed account deferral reached the service manager")
    with pytest.raises(publication.StartupPublicationError, match="unit mismatch"):
        publication.activate(layout[1], layout[3], run=run)


def test_activation_rejects_foreign_manager_unit(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json")
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"/foreign/unit", b"")
    with pytest.raises(publication.StartupPublicationError, match="different unit"):
        publication.activate(layout[1], layout[3], run=run)
    assert not any("enable" in argv for argv in seen)


def test_activation_rejects_late_unit_replacement_before_start(layout):
    publication.publish(bundle(layout), *layout[1:4], layout[4] / "backup.json")
    unit = layout[3] / publication.SERVICE
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        unit.write_bytes(unit.read_bytes() + b"# concurrent edit\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    with pytest.raises(publication.StartupPublicationError, match="changed during activation"):
        publication.activate(layout[1], layout[3], run=run)
    assert len(seen) == 1


def test_backup_targets_cannot_redirect_inverse(layout):
    backup = layout[4] / "backup.json"
    publication.publish(bundle(layout), *layout[1:4], backup)
    payload = json.loads(backup.read_bytes())
    payload["preimages"][str(layout[4] / "foreign")] = payload["preimages"].pop(str(layout[3] / publication.SERVICE))
    backup.write_bytes(publication._json(payload))
    with pytest.raises(publication.StartupPublicationError, match="targets are invalid"):
        publication.rollback(backup, layout[1], layout[3])
