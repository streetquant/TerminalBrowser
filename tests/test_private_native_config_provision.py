from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from termdeck import codex_auth, codex_config_scope as config, native_config_view as view
from termdeck.models import SessionRecord
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import (
    TmuxBackend, TmuxError, _auth_legacy_lifetime_lock_path, _auth_lifetime_lock_path,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path, skip=()):
    result = {}
    for item in sorted(path.rglob("*")):
        relative = str(item.relative_to(path))
        if any(relative == name or relative.startswith(name + "/") for name in skip):
            continue
        info = item.lstat()
        data = os.readlink(item) if item.is_symlink() else item.read_bytes() if item.is_file() else None
        result[relative] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink, data)
    return result


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "resolve_codex_launcher", lambda *_a, **_k: None)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "config.toml").write_text('model = "fixture"\n')
    (source / "config.toml").chmod(0o600)
    (source / "history.jsonl").write_bytes(b"source history must stay unchanged\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3",
                         auth_root=tmp_path / "auth", account_source=source,
                         provision_auth_scopes=False)
    record = store.create("Manual A", "owned", "/bin/sh", str(workspace), automatic_name=False)
    record = store.ensure_auth_scope(record.id, private_history=True)
    neighbor = store.create("Manual B", "owned", "/bin/sh", str(workspace), automatic_name=False)
    neighbor = store.ensure_auth_scope(neighbor.id, private_history=True)
    home = Path(record.codex_home)
    for name in ("history.jsonl", "state_5.sqlite"):
        (home / name).write_bytes(b"private synthetic state\n")
        (home / name).chmod(0o600)
    backend = TmuxBackend(executable="/usr/bin/tmux")
    monkeypatch.setattr(backend, "_auth_session_exists", lambda _r: False)
    monkeypatch.setattr(backend, "_run", lambda *_a, **_k: pytest.fail("config-only API must not launch/control a terminal"))
    yield store, record, backend, home, source, Path(neighbor.codex_home)
    store.close()


def prepare(owned, **kwargs):
    store, record, backend, _home, source, _neighbor = owned
    return config.prepare_private_native_configuration(
        store, record.id, backend=backend,
        expected_source_sha256=kwargs.pop("expected_source_sha256", digest(source / "config.toml")),
        **kwargs,
    )


def snapshot(owned):
    store, _record, _backend, home, source, neighbor = owned
    ignored = ("config.toml", view.RECEIPT, ".termdeck-config.lock",
               ".termdeck-auth-transaction.lock", ".termdeck-codex-lifetime")
    return (tuple(tuple(row) for row in store.conn.execute("SELECT * FROM sessions ORDER BY id")),
            tree(home, ignored), tree(source), tree(neighbor))


def private_state(home):
    return tree(home, (".termdeck-config.lock", ".termdeck-auth-transaction.lock",
                       ".termdeck-codex-lifetime"))


def test_explicit_new_call_confines_only_configuration_and_preserves_every_other_plane(owned, monkeypatch):
    store, record, _backend, home, source, _neighbor = owned
    before = snapshot(owned)
    monkeypatch.setattr(store, "ensure_auth_scope", lambda *_a, **_k: pytest.fail("must not reprepare scope"))
    monkeypatch.setattr(codex_auth, "prepare_auth_scope", lambda *_a, **_k: pytest.fail("must not reprovision credentials"))
    assert prepare(owned) == "initialized"
    assert snapshot(owned) == before
    assert not (home / "config.toml").is_symlink()
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
    assert view.is_managed_private_config(home, source)
    assert store.get(record.id).name == "Manual A"
    assert store.conn.in_transaction is False


def test_private_native_atomic_edit_ready_reprepare_retains_inode_and_exact_bytes(owned):
    _store, _record, _backend, home, _source, _neighbor = owned
    prepare(owned)
    replacement = home / "native-edit"
    replacement.write_bytes((home / "config.toml").read_bytes() + b"\n[tui.model_availability_nux]\nfixture = 1\n")
    replacement.chmod(0o600)
    replacement.replace(home / "config.toml")
    before = private_state(home), snapshot(owned)
    assert prepare(owned) == "ready"
    assert (private_state(home), snapshot(owned)) == before


def test_changed_source_and_private_edit_conflict_does_not_overwrite_either(owned):
    _store, _record, _backend, home, source, _neighbor = owned
    prepare(owned)
    with (home / "config.toml").open("ab") as stream:
        stream.write(b"\n[tui]\nfixture = 1\n")
    (source / "config.toml").write_text('model = "changed"\n')
    before = private_state(home), snapshot(owned)
    with pytest.raises(config.CodexConfigScopeError, match="private configuration edits"):
        prepare(owned)
    assert (private_state(home), snapshot(owned)) == before


def test_explicit_source_cas_refuses_changed_preimage_before_receipt_or_config_write(owned):
    _store, _record, _backend, home, source, _neighbor = owned
    expected = digest(source / "config.toml")
    (source / "config.toml").write_text('model = "successor"\n')
    before = private_state(home), snapshot(owned)
    with pytest.raises(config.CodexConfigScopeError, match="expected source digest"):
        prepare(owned, expected_source_sha256=expected)
    assert (private_state(home), snapshot(owned)) == before
    assert not (home / view.RECEIPT).exists()


@pytest.mark.parametrize("expected", [None, "", "0" * 63, "A" * 64, "0" * 64 + "\n", 7])
def test_required_exact_digest_refuses_before_any_new_gate_or_object(owned, expected):
    _store, _record, _backend, home, _source, _neighbor = owned
    before = tree(home)
    with pytest.raises(config.CodexConfigScopeError, match="exact expected"):
        prepare(owned, expected_source_sha256=expected)
    assert tree(home) == before


@pytest.mark.parametrize("object_name", ["store", "backend"])
def test_arbitrary_ownership_or_quiescence_objects_are_not_accepted(owned, object_name):
    store, record, backend, home, source, _neighbor = owned
    before = tree(home)
    if object_name == "store":
        store = object()
    else:
        backend = object()
    with pytest.raises(config.CodexConfigScopeError, match="concrete owning"):
        config.prepare_private_native_configuration(store, record.id, backend=backend,
                                                    expected_source_sha256=digest(source / "config.toml"))
    assert tree(home) == before


def test_default_shared_api_and_legacy_physical_config_are_not_opted_in(owned):
    _store, _record, _backend, home, source, _neighbor = owned
    config.prepare_shared_codex_configuration(home, source)
    assert (home / "config.toml").is_symlink()
    assert not (home / view.RECEIPT).exists()
    (home / "config.toml").unlink()
    (home / "config.toml").write_text('model = "legacy"\n')
    (home / "config.toml").chmod(0o600)
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError, match="no TermDeck provenance"):
        prepare(owned)
    assert private_state(home) == before


@pytest.mark.parametrize("mutation", ["missing", "foreign-bytes", "symlink", "hardlink", "unsafe-mode"])
def test_private_history_marker_must_be_real_safe_owned_provenance(owned, mutation):
    _store, _record, _backend, home, _source, _neighbor = owned
    marker = home / ".termdeck-private-history"
    if mutation == "missing":
        marker.unlink()
    elif mutation == "foreign-bytes":
        marker.write_bytes(b"foreign\n")
    elif mutation == "unsafe-mode":
        marker.chmod(0o666)
    else:
        foreign = home.parent / "marker"
        foreign.write_bytes(marker.read_bytes())
        foreign.chmod(0o600)
        marker.unlink()
        marker.symlink_to(foreign) if mutation == "symlink" else os.link(foreign, marker)
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError):
        prepare(owned)
    assert private_state(home) == before


@pytest.mark.parametrize("which", ["auth-root", "home"])
def test_scope_directory_alias_is_not_owning_physical_authority(owned, which):
    store, record, _backend, home, _source, _neighbor = owned
    if which == "auth-root":
        alias = store.auth_root.with_name("auth-alias")
        alias.symlink_to(store.auth_root, target_is_directory=True)
        store.auth_root = alias
    else:
        preserved = home.with_name(home.name + "-saved")
        home.rename(preserved)
        home.symlink_to(preserved, target_is_directory=True)
    before = private_state(home.resolve())
    with pytest.raises(config.CodexConfigScopeError):
        prepare(owned)
    assert private_state(home.resolve()) == before


def test_missing_home_is_not_fabricated(owned):
    _store, _record, _backend, home, _source, _neighbor = owned
    saved = home.with_name(home.name + "-saved")
    home.rename(saved)
    with pytest.raises(config.CodexConfigScopeError, match="missing"):
        prepare(owned)
    assert not home.exists()
    assert not (saved / view.RECEIPT).exists()


def test_wrong_auth_root_or_account_source_refuses_without_any_scope_write(owned):
    store, _record, _backend, home, _source, _neighbor = owned
    wrong = home.parent.parent / "wrong"
    wrong.mkdir(mode=0o700)
    before = private_state(home)
    original = store.auth_root
    store.auth_root = wrong
    with pytest.raises(config.CodexConfigScopeError, match="owning auth root"):
        prepare(owned)
    store.auth_root = original
    store.account_source = wrong
    with pytest.raises(config.CodexConfigScopeError, match="owning account source"):
        prepare(owned)
    assert private_state(home) == before


def test_missing_record_or_unscoped_record_cannot_create_a_config_home(owned):
    store, record, backend, home, source, _neighbor = owned
    bare = store.create("No scope", "owned", "/bin/sh", record.cwd)
    before = private_state(home)
    for key in ("td-absent", bare.id):
        with pytest.raises(config.CodexConfigScopeError, match="internal saved"):
            config.prepare_private_native_configuration(store, key, backend=backend,
                                                        expected_source_sha256=digest(source / "config.toml"))
    assert private_state(home) == before
    assert store.get(bare.id).codex_home is None


def test_external_saved_record_is_refused(owned):
    store, record, _backend, home, _source, _neighbor = owned
    store.update(record.id, external=True)
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError, match="internal saved"):
        prepare(owned)
    assert private_state(home) == before


def test_live_session_refuses_before_snapshot(owned, monkeypatch):
    _store, _record, backend, home, _source, _neighbor = owned
    monkeypatch.setattr(backend, "_auth_session_exists", lambda _r: True)
    before = private_state(home), snapshot(owned)
    with pytest.raises(config.CodexConfigScopeError, match="still live"):
        prepare(owned)
    assert (private_state(home), snapshot(owned)) == before


def test_present_saved_process_is_not_accepted_as_stopped(owned):
    store, record, _backend, home, _source, _neighbor = owned
    store.update(record.id, last_pid=os.getpid())
    before = tree(home)
    with pytest.raises(config.CodexConfigScopeError, match="process is still present"):
        prepare(owned)
    assert tree(home) == before


@pytest.mark.parametrize("generation", ["legacy", "current"])
def test_each_native_writer_generation_refuses_and_does_not_replace_config(owned, generation):
    _store, record, _backend, home, _source, _neighbor = owned
    path = _auth_legacy_lifetime_lock_path(record) if generation == "legacy" else _auth_lifetime_lock_path(record)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        before = private_state(home), snapshot(owned)
        with pytest.raises(TmuxError):
            prepare(owned)
        assert (private_state(home), snapshot(owned)) == before
    finally:
        os.close(fd)


def test_changed_record_during_quiescence_is_refused(owned, monkeypatch):
    store, record, backend, home, _source, _neighbor = owned
    original = backend._auth_session_exists
    changed = False
    def change(candidate):
        nonlocal changed
        if not changed:
            store.update(record.id, name="Concurrent rename")
            changed = True
        return original(candidate)
    monkeypatch.setattr(backend, "_auth_session_exists", change)
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError, match="Saved terminal changed"):
        prepare(owned)
    assert private_state(home) == before
    assert store.get(record.id).name == "Concurrent rename"


def test_registry_transaction_excludes_other_row_writers_through_publication(owned, monkeypatch):
    store, record, _backend, _home, _source, _neighbor = owned
    owning = view.prepare_private_config
    checked = []
    def guarded(*args, **kwargs):
        connection = sqlite3.connect(store.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("UPDATE sessions SET name=? WHERE id=?", ("Foreign rename", record.id))
            checked.append(True)
        finally:
            connection.close()
        return owning(*args, **kwargs)
    monkeypatch.setattr(view, "prepare_private_config", guarded)
    assert prepare(owned) == "initialized"
    assert checked == [True]
    assert store.get(record.id) == record
    assert not store.conn.in_transaction


def test_home_replaced_after_initial_authority_is_refused_before_publication(owned, monkeypatch):
    _store, _record, backend, home, _source, _neighbor = owned
    original = backend._auth_session_exists
    moved = False
    def replace_home(candidate):
        nonlocal moved
        if not moved:
            saved = home.with_name(home.name + "-old")
            home.rename(saved)
            home.mkdir(mode=0o700)
            for node in saved.iterdir():
                if node.is_symlink():
                    (home / node.name).symlink_to(os.readlink(node), target_is_directory=node.is_dir())
                elif node.is_file():
                    (home / node.name).write_bytes(node.read_bytes())
                    (home / node.name).chmod(node.stat().st_mode & 0o777)
                elif node.name == ".termdeck-codex-lifetime":
                    (home / node.name).mkdir(mode=0o700)
            moved = True
        return original(candidate)
    monkeypatch.setattr(backend, "_auth_session_exists", replace_home)
    with pytest.raises(config.CodexConfigScopeError, match="authority changed"):
        prepare(owned)
    assert (home / "config.toml").is_symlink()
    assert not (home / view.RECEIPT).exists()


@pytest.mark.parametrize("target_name", ["private-history", "runtime", "ledger"])
def test_canonical_source_alias_into_protected_storage_is_refused(owned, target_name):
    store, _record, _backend, home, source, _neighbor = owned
    if target_name == "private-history":
        target = home / "history.jsonl"
    elif target_name == "runtime":
        target = store.path.parent / "runtime" / "protected.toml"
        target.parent.mkdir(mode=0o700)
        target.write_text('model = "protected"\n')
    else:
        target = source.parent / ".beads" / "protected.toml"
        target.parent.mkdir(mode=0o700)
        target.write_text('model = "protected"\n')
    target.chmod(0o600)
    (source / "config.toml").unlink()
    (source / "config.toml").symlink_to(target)
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError):
        prepare(owned)
    assert private_state(home) == before


def test_source_hardlink_is_refused_without_receipt_publication(owned):
    _store, _record, _backend, home, source, _neighbor = owned
    os.link(source / "config.toml", source / "second-link")
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError):
        prepare(owned)
    assert private_state(home) == before
    assert not (home / view.RECEIPT).exists()


def test_existing_registry_transaction_is_not_rolled_back_or_used(owned):
    store, _record, _backend, home, _source, _neighbor = owned
    store.conn.execute("BEGIN IMMEDIATE")
    before = tree(home)
    with pytest.raises(config.CodexConfigScopeError, match="idle registry"):
        prepare(owned)
    assert store.conn.in_transaction
    assert tree(home) == before
    store.conn.rollback()


def test_optional_engine_digest_keeps_legacy_call_shape_and_rejects_wrong_digest(owned):
    _store, _record, _backend, home, source, _neighbor = owned
    before = private_state(home)
    with pytest.raises(config.CodexConfigScopeError, match="expected source digest"):
        view.prepare_private_config(home, source, source / "config.toml", expected_source_sha256="0" * 64)
    assert private_state(home) == before
    assert view.prepare_private_config(home, source, source / "config.toml") == "initialized"
