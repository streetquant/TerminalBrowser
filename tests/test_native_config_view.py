from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from termdeck import native_config_view as view
from termdeck.codex_config_scope import CodexConfigScopeError, prepare_shared_codex_configuration


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    monkeypatch.setattr("termdeck.codex_config_scope.resolve_codex_launcher", lambda *_a, **_k: None)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    config = source / "config.toml"
    config.write_text('model = "system-default"\n')
    config.chmod(0o600)
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    prepare_shared_codex_configuration(home, source)
    return home, source, config


def prepare(scoped, **kwargs):
    home, source, config = scoped
    return view.prepare_private_config(home, source, config.resolve() if config.exists() else None, **kwargs)


def native_edit(home, extra='\n[projects."/approved/workspace"]\ntrust_level = "trusted"\n'):
    temporary = home / "native-config-write.tmp"
    temporary.write_bytes((home / "config.toml").read_bytes() + extra.encode())
    temporary.chmod(0o600)
    temporary.replace(home / "config.toml")


def snapshot(home):
    return {p.name: (p.lstat().st_ino, p.lstat().st_mode,
                     os.readlink(p) if p.is_symlink() else p.read_bytes())
            for p in home.iterdir() if p.is_file() or p.is_symlink()}


def test_native_atomic_trust_writes_survive_repeated_scope_and_launch_preparation(scoped):
    home, source, config = scoped
    original = config.read_bytes()
    assert prepare(scoped, write=False) == "initialization-required"
    assert (home / "config.toml").is_symlink()
    assert not (home / view.RECEIPT).exists()
    assert prepare(scoped) == "initialized"
    assert not (home / "config.toml").is_symlink()
    native_edit(home)
    before = snapshot(home)
    status = prepare_shared_codex_configuration(home, source)
    assert status.configuration_ready and not status.migration_required
    assert view.is_managed_private_config(home, source)
    assert prepare(scoped, write=False) == "ready"
    assert prepare(scoped) == "ready"
    # The existing shared preparer may touch its lock; configuration bytes and
    # inode identity must survive native's atomic replacement.
    assert snapshot(home)["config.toml"] == before["config.toml"]
    assert snapshot(home)[view.RECEIPT] == before[view.RECEIPT]
    assert config.read_bytes() == original


def test_tabs_receive_independent_private_configurations(scoped):
    home, source, config = scoped
    sibling = home.with_name("sibling")
    sibling.mkdir(mode=0o700)
    prepare_shared_codex_configuration(sibling, source)
    prepare(scoped)
    view.prepare_private_config(sibling, source, config)
    sibling_before = snapshot(sibling)
    native_edit(home)
    assert snapshot(sibling) == sibling_before
    assert "trust_level" not in (sibling / "config.toml").read_text()
    assert view.is_managed_private_config(home, source)
    assert view.is_managed_private_config(sibling, source)
    assert json.loads((home / view.RECEIPT).read_text())["identity"] != json.loads((sibling / view.RECEIPT).read_text())["identity"]


def test_changed_defaults_refresh_only_an_untouched_private_copy(scoped):
    home, source, config = scoped
    prepare(scoped)
    config.write_text('model = "new-system-default"\n')
    before = snapshot(home)
    assert prepare(scoped, write=False) == "refresh-required"
    assert snapshot(home) == before
    assert prepare(scoped) == "refreshed"
    assert (home / "config.toml").read_bytes().endswith(config.read_bytes())
    native_edit(home)
    config.write_text('model = "third-system-default"\n')
    before = snapshot(home)
    assert prepare_shared_codex_configuration(home, source).configuration_ready
    for write in (False, True):
        with pytest.raises(CodexConfigScopeError, match="System Codex configuration changed"):
            prepare(scoped, write=write)
        assert snapshot(home) == before
    assert config.read_text() == 'model = "third-system-default"\n'


@pytest.mark.parametrize("which", ["config", "receipt"])
@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "permissions", "directory", "fifo", "foreign"])
def test_unsafe_or_foreign_private_objects_are_never_adopted(scoped, which, mutation):
    home, source, _ = scoped
    prepare(scoped)
    path = home / ("config.toml" if which == "config" else view.RECEIPT)
    foreign = home.with_name("foreign-data")
    original = path.read_bytes()
    foreign.write_bytes(original)
    foreign.chmod(0o600)
    path.unlink()
    if mutation == "symlink":
        path.symlink_to(foreign)
    elif mutation == "hardlink":
        os.link(foreign, path)
    elif mutation == "directory":
        path.mkdir(mode=0o700)
    elif mutation == "fifo":
        os.mkfifo(path, 0o600)
    elif mutation == "permissions":
        path.write_bytes(original)
        path.chmod(0o644)
    else:
        if which == "config":
            changed = original.replace(json.loads((home / view.RECEIPT).read_text())["identity"].encode(), b"a" * 32, 1)
        else:
            value = json.loads(original)
            value["home"] = str(home.with_name("other-tab"))
            changed = json.dumps(value).encode()
        path.write_bytes(changed)
        path.chmod(0o600)
    before = path.lstat()
    for action in (lambda: view.is_managed_private_config(home, source), lambda: prepare(scoped)):
        with pytest.raises(CodexConfigScopeError):
            action()
    assert path.lstat().st_ino == before.st_ino
    assert foreign.read_bytes() == original


def test_unreceipted_regular_configuration_is_preserved_and_refused(scoped):
    home, source, _ = scoped
    (home / "config.toml").unlink()
    (home / "config.toml").write_text('model = "private-legacy"\n')
    (home / "config.toml").chmod(0o600)
    before = snapshot(home)
    assert not view.is_managed_private_config(home, source)
    with pytest.raises(CodexConfigScopeError, match="provenance"):
        prepare(scoped)
    assert snapshot(home) == before
    assert prepare_shared_codex_configuration(home, source).migration_required


def test_managed_view_does_not_bless_unrelated_private_configuration_conflicts(scoped):
    home, source, _ = scoped
    prepare(scoped)
    for directory, value in ((source, "system"), (home, "private")):
        path = directory / "extra.config.toml"
        path.write_text(f'fixture = "{value}"\n')
        path.chmod(0o600)
    before = snapshot(home)
    status = prepare_shared_codex_configuration(home, source)
    assert status.migration_required and not status.configuration_ready
    assert snapshot(home)["extra.config.toml"] == before["extra.config.toml"]
    assert view.is_managed_private_config(home, source)


@pytest.mark.parametrize("stage", ["before-config", "before-ready"])
@pytest.mark.parametrize("refresh", [False, True])
def test_interrupted_publication_recovers_only_its_unchanged_private_payload(scoped, monkeypatch, stage, refresh):
    home, source, config = scoped
    if refresh:
        prepare(scoped)
        config.write_text('model = "changed-default"\n')
    original = view._publish
    count = 0
    def interrupted(tree, name, raw, expected):
        nonlocal count
        count += 1
        if count == (2 if stage == "before-config" else 3):
            raise OSError("disposable publication interruption")
        return original(tree, name, raw, expected)
    with monkeypatch.context() as patch:
        patch.setattr(view, "_publish", interrupted)
        with pytest.raises(CodexConfigScopeError):
            prepare(scoped)
    assert json.loads((home / view.RECEIPT).read_text())["state"] == "pending"
    assert prepare_shared_codex_configuration(home, source).configuration_ready
    assert prepare(scoped) == "refreshed"
    assert json.loads((home / view.RECEIPT).read_text())["state"] == "ready"
    assert (home / "config.toml").read_bytes().endswith(config.read_bytes())


def test_native_edit_during_unfinished_refresh_is_preserved(scoped, monkeypatch):
    home, _, config = scoped
    prepare(scoped)
    config.write_text('model = "new-default"\n')
    original = view._publish
    def interrupted(tree, name, raw, expected):
        if name == "config.toml":
            raise OSError("disposable publication interruption")
        return original(tree, name, raw, expected)
    with monkeypatch.context() as patch:
        patch.setattr(view, "_publish", interrupted)
        with pytest.raises(CodexConfigScopeError):
            prepare(scoped)
    native_edit(home)
    before = snapshot(home)
    with pytest.raises(CodexConfigScopeError, match="unfinished refresh"):
        prepare(scoped)
    assert snapshot(home) == before


def test_native_replacement_before_publication_is_never_overwritten(scoped, monkeypatch):
    home, _, config = scoped
    prepare(scoped)
    config.write_text('model = "new-default"\n')
    original = view._publish
    preserved = None
    def racing(tree, name, raw, expected):
        nonlocal preserved
        if name == "config.toml":
            native_edit(home)
            preserved = (home / name).read_bytes()
        return original(tree, name, raw, expected)
    monkeypatch.setattr(view, "_publish", racing)
    with pytest.raises(CodexConfigScopeError, match="before publication"):
        prepare(scoped)
    assert (home / "config.toml").read_bytes() == preserved


def test_missing_source_config_supports_private_native_trust_and_repeated_preparation(scoped):
    home, source, config = scoped
    (home / "config.toml").unlink()
    config.unlink()
    assert prepare(scoped) == "initialized"
    native_edit(home)
    before = snapshot(home)
    assert prepare_shared_codex_configuration(home, source).configuration_ready
    assert prepare(scoped) == "ready"
    assert snapshot(home)["config.toml"] == before["config.toml"]
    config.write_text('model = "appeared-default"\n')
    config.chmod(0o600)
    with pytest.raises(CodexConfigScopeError, match="System Codex configuration changed"):
        prepare(scoped)


def test_unsafe_source_and_wrong_scope_are_refused_before_cloning(scoped, monkeypatch):
    home, source, config = scoped
    other = config.with_name("hardlinked")
    os.link(config, other)
    with pytest.raises(CodexConfigScopeError, match="unsafe authority"):
        prepare(scoped)
    assert not (home / view.RECEIPT).exists()
    other.unlink()
    alias = home.with_name("private-alias")
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(CodexConfigScopeError):
        view.prepare_private_config(alias, source, config)
    with pytest.raises(CodexConfigScopeError):
        view.prepare_private_config(source, source, config)
    monkeypatch.setattr(os, "geteuid", lambda: 123456)
    with pytest.raises(CodexConfigScopeError):
        prepare(scoped)


def test_concurrent_preparation_serializes_one_private_view(scoped):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: prepare(scoped), range(2)))
    assert sorted(results) == ["initialized", "ready"]
    home, source, _ = scoped
    assert view.is_managed_private_config(home, source)
    assert not list(home.glob(".termdeck-native-config-*.tmp"))


def test_invalid_or_oversized_toml_does_not_publish_a_view(scoped):
    home, _, config = scoped
    for raw in (b'invalid = [', b'#' * view.MAX_CONFIG_BYTES):
        config.write_bytes(raw)
        with pytest.raises(CodexConfigScopeError):
            prepare(scoped)
        assert not (home / view.RECEIPT).exists()
        assert (home / "config.toml").is_symlink()
