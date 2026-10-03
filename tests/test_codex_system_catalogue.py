"""Synthetic codex-auth contracts; no host credential or provider access."""
import base64
import json
import os
from pathlib import Path
import stat

import pytest

from termdeck import codex_auth as auth
from termdeck import codex_catalogue as catalogue
from termdeck.codex_credentials import read
from termdeck.models import SessionRecord


def credential(account="alpha", refresh="2026-01-01T00:00:00Z", token="fixture"):
    claims = {"sub": f"fixture-user-{account}", "iss": "fixture",
              "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"tokens": {"id_token": f"e30.{encoded}.fixture", "account_id": account,
                       "refresh_token": token}, "last_refresh": refresh}


def write(path, value):
    raw = value if isinstance(value, str) else json.dumps(value)
    path.write_text(raw)
    path.chmod(0o600)


def replace_auth(home, value):
    temporary = home / ".fixture-auth"
    write(temporary, value)
    os.replace(temporary, home / "auth.json")


def profile(root, name, value=None):
    parent = root / "login-homes"
    parent.mkdir(mode=0o700, exist_ok=True)
    home = parent / name
    home.mkdir(mode=0o700)
    write(home / "auth.json", credential(name.removeprefix("login-")) if value is None else value)
    return home


def saved(root, name, value=None):
    parent = root / "accounts"
    parent.mkdir(mode=0o700, exist_ok=True)
    write(parent / f"{name}.json", credential(name) if value is None else value)
    return parent / f"{name}.json"


def record(home):
    return SessionRecord(id="fixture", name="fixture", tag="ai", command="bash",
                         cwd=str(home.parent), tmux_name="fixture", created_at="now",
                         last_opened_at="now", auth_scope_id="auth-fixture", codex_home=str(home))


def tree(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        value = ("link", os.readlink(path)) if path.is_symlink() else (
            ("directory",) if path.is_dir() else ("file", path.read_bytes()))
        result[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), value)
    return result


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "system"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(auth, "_real_codex_auth", lambda _: None)
    # Wrappers are separately covered; tests never discover a host executable.
    monkeypatch.setattr(auth, "refresh_auth_scope_wrappers", lambda _: None)
    return root


def prepare(source, name="tab", **kwargs):
    return auth.prepare_auth_scope(source.parent / name, account_source=source, **kwargs)


def test_selected_login_home_overrides_root_auth_and_inherited_environment(source, monkeypatch):
    old = saved(source, "alpha")
    selected = profile(source, "login-beta")
    (source / "auth.json").symlink_to(old)
    write(source / "current", "alpha\n")
    write(source / "next-login-home", str(selected) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(source.parent / "unrelated"))
    before = tree(source)
    home = prepare(source)
    state = auth.read_auth_state(home, account_source=source)
    assert state.canonical_account_key == state.selected_account_key == "login-beta"
    assert state.canonical_credential_status == state.credential_status == "current"
    assert state.canonical_binding_ready
    assert not (home / "accounts" / "login-beta.json").is_symlink()
    assert tree(source) == before


def test_profile_only_source_and_aliases_share_one_identity(source):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    write(source / "account-profiles.json", {"work": str(selected), "team": str(selected)})
    before = tree(source)
    state = auth.read_auth_state(prepare(source), account_source=source)
    assert len(state.accounts) == 1
    assert state.accounts[0].label == "team"
    for selector in ("work", "TEAM", "login-alpha"):
        assert auth.resolve_account(state, selector).account_key == "login-alpha"
    assert not (source / "accounts").exists()
    assert "fixture-user" not in json.dumps(state.to_dict())
    assert "refresh_token" not in json.dumps(state.to_dict())
    assert tree(source) == before


def test_alias_supersedes_legacy_copy_with_ordered_newer_snapshot(source):
    saved(source, "work", credential())
    selected = profile(source, "login-alpha", credential(refresh="2026-02-01T00:00:00Z", token="new"))
    write(source / "account-profiles.json", {"work": str(selected)})
    write(source / "next-login-home", str(selected))
    state = auth.read_auth_state(prepare(source), account_source=source)
    assert [item.account_key for item in state.accounts] == ["work"]
    assert state.selected_account_key == "work"
    assert read(Path(state.codex_home) / "accounts" / "work.json").digest == read(selected / "auth.json").digest


def test_system_legacy_profile_link_is_pinned_to_saved_account(source):
    target = saved(source, "alpha")
    selected = profile(source, "login-alpha")
    (selected / "auth.json").unlink()
    (selected / "auth.json").symlink_to(target)
    write(source / "account-profiles.json", {"work": str(selected)})
    write(source / "next-login-home", str(selected))
    before = tree(source)
    state = auth.read_auth_state(prepare(source), account_source=source)
    assert len(state.accounts) == 1 and state.selected_account_key == "alpha"
    assert auth.resolve_account(state, "work").account_key == "alpha"
    assert tree(source) == before


def test_stale_selected_home_is_visible_and_does_not_set_new_tab_default(source):
    saved(source, "alpha", credential(refresh="2026-02-01T00:00:00Z", token="new"))
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    state = auth.read_auth_state(prepare(source), account_source=source)
    assert state.canonical_account_key == "alpha"
    assert state.canonical_credential_status == "stale"
    assert state.selected_account_key is None


def test_marker_is_not_identity_evidence(source):
    selected = profile(source, "login-alpha", credential("beta"))
    write(source / "next-login-home", str(selected))
    write(source / "current", "wrong-identity")
    root = saved(source, "beta")
    write(source / "auth.json", credential("other"))
    state = auth.read_auth_state(prepare(source), account_source=source)
    assert state.canonical_account_key == "beta"
    assert read(root).identity == read(selected / "auth.json").identity


@pytest.mark.parametrize("raw", ["[]", "{", '{"x":"a","x":"b"}', '{"work":2}',
                                      '{"../bad":"/tmp"}', "x" * 65537])
def test_malformed_alias_metadata_refused(source, raw):
    profile(source, "login-alpha")
    write(source / "account-profiles.json", raw)
    before = tree(source)
    with pytest.raises(auth.CodexAuthError):
        auth.read_auth_state(source.parent / "tab", account_source=source)
    assert tree(source) == before


@pytest.mark.parametrize("kind", ["outside", "relative", "traversal", "missing", "profile-link",
                                      "parent-link", "auth-escape", "auth-chain", "metadata-link",
                                      "metadata-mode", "credential-mode", "hardlink", "case-collision"])
def test_unsafe_profile_or_metadata_is_refused(source, kind):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    outside = source.parent / "outside"
    outside.mkdir(mode=0o700)
    write(outside / "auth.json", credential("outside"))
    if kind in {"outside", "relative", "traversal", "missing"}:
        paths = {"outside": str(outside), "relative": "login-homes/login-alpha",
                 "traversal": str(selected / ".." / "login-alpha"), "missing": str(selected.parent / "login-missing")}
        write(source / "next-login-home", paths[kind])
    elif kind == "profile-link":
        selected.rename(source / "held")
        selected.symlink_to(outside, target_is_directory=True)
    elif kind == "parent-link":
        selected.parent.rename(source / "held")
        (source / "login-homes").symlink_to(source / "held", target_is_directory=True)
    elif kind in {"auth-escape", "auth-chain"}:
        (selected / "auth.json").unlink()
        target = outside / "auth.json"
        if kind == "auth-chain":
            (source / "auth.json").symlink_to(target)
            target = source / "auth.json"
        (selected / "auth.json").symlink_to(target)
    elif kind == "metadata-link":
        (source / "next-login-home").rename(source / "held-marker")
        (source / "next-login-home").symlink_to(source / "held-marker")
    elif kind == "metadata-mode":
        (source / "next-login-home").chmod(0o666)
    elif kind == "credential-mode":
        (selected / "auth.json").chmod(0o666)
    elif kind == "hardlink":
        os.link(selected / "auth.json", source / "hardlink")
    else:
        write(source / "account-profiles.json", {"work": str(selected), "WORK": str(selected)})
    with pytest.raises(auth.CodexAuthError):
        auth.read_auth_state(source.parent / "tab", account_source=source)


@pytest.mark.parametrize("conflict", ["identity", "equal-time", "no-time", "malformed"])
def test_conflicting_source_snapshots_fail_closed(source, conflict):
    saved(source, "work")
    value = credential("work", token="other")
    if conflict == "identity":
        value = credential("another")
    elif conflict == "no-time":
        value.pop("last_refresh")
    elif conflict == "malformed":
        value = {"tokens": {"account_id": "work"}}
    if conflict == "identity":
        # A native alias may supersede a legacy saved name, but it cannot
        # give a different identity to an existing physical-home selector.
        profile(source, "login-work", credential("work"))
        selected = profile(source, "login-other", value)
        alias = "login-work"
    else:
        selected = profile(source, "login-work", value)
        alias = "work"
    write(source / "account-profiles.json", {alias: str(selected)})
    with pytest.raises(auth.CodexAuthError):
        prepare(source)


def test_refreshed_private_account_survives_switch_away_and_back_without_source_writes(source):
    selected = profile(source, "login-alpha")
    profile(source, "login-beta")
    write(source / "next-login-home", str(selected))
    first, second = prepare(source, "one"), prepare(source, "two")
    before, other = tree(source), tree(second)
    newer = credential(refresh="2026-03-01T00:00:00Z", token="private-rotation")
    replace_auth(first, newer)
    manager = auth.CodexAuthManager(account_source=source)
    assert manager.state(record(first)).credential_status == "refreshed"
    assert manager.switch(record(first), "login-beta").ok
    assert manager.switch(record(first), "login-alpha").ok
    assert read((first / "auth.json").resolve()).digest == read(first / "accounts" / "login-alpha.json").digest
    assert json.loads((first / "auth.json").read_bytes()) == newer
    assert manager.state(record(first)).canonical_binding_ready
    assert manager.state(record(second)).selected_account_key == "login-alpha"
    assert tree(second) == other
    assert tree(source) == before
    assert not (source / ".termdeck-account-authority.json").exists()


def test_newer_source_snapshot_updates_only_quiescent_private_copy(source):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    home = prepare(source)
    write(selected / "auth.json", credential(refresh="2026-04-01T00:00:00Z", token="source-new"))
    before = tree(home)
    manager = auth.CodexAuthManager(account_source=source)
    assert manager.state(record(home)).credential_status == "stale"
    assert tree(home) == before
    system = tree(source)
    assert manager.switch(record(home), "login-alpha").ok
    assert read(home / "accounts" / "login-alpha.json").digest == read(selected / "auth.json").digest
    assert tree(source) == system


@pytest.mark.parametrize("mutation", ["revoke", "identity", "conflict"])
def test_private_cache_cannot_restore_revoked_or_conflicting_identity(source, mutation):
    selected = profile(source, "login-alpha")
    profile(source, "login-beta")
    write(source / "next-login-home", str(selected))
    home = prepare(source)
    replace_auth(home, credential(refresh="2026-03-01T00:00:00Z", token="private"))
    manager = auth.CodexAuthManager(account_source=source)
    assert manager.switch(record(home), "login-beta").ok
    if mutation == "revoke":
        write(source / "next-login-home", str(source / "login-homes" / "login-beta"))
        (selected / "auth.json").unlink()
    elif mutation == "identity":
        write(selected / "auth.json", credential("someone-else"))
    else:
        write(selected / "auth.json", credential(refresh="2026-03-01T00:00:00Z", token="conflict"))
    before, system = tree(home), tree(source)
    if mutation == "revoke":
        result = manager.switch(record(home), "login-alpha")
        assert not result.ok
    else:
        with pytest.raises(auth.CodexAuthError):
            manager.switch(record(home), "login-alpha")
    assert tree(home) == before and tree(source) == system


def test_missing_selected_profile_never_falls_back_to_root(source):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    write(source / "auth.json", credential())
    (selected / "auth.json").unlink()
    with pytest.raises(auth.CodexAuthError, match="revoked"):
        prepare(source)


def test_existing_native_scope_migrates_with_same_identity_and_private_selection(source):
    original = saved(source, "alpha")
    write(source / "auth.json", credential())
    home = prepare(source)
    assert (home / "accounts").is_symlink()
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    before = tree(source)
    manager = auth.CodexAuthManager(account_source=source)
    assert not manager.state(record(home)).canonical_binding_ready
    assert manager.switch(record(home), "alpha").ok
    assert not (home / "accounts").is_symlink()
    assert read(home / "accounts" / "alpha.json").digest == read(original).digest
    assert tree(source) == before


@pytest.mark.parametrize("failure", ["binding", "after-binding", "partial-cleanup"])
def test_failed_private_catalogue_publication_restores_previous_tree_and_auth(source, monkeypatch, failure):
    selected = profile(source, "login-alpha")
    profile(source, "login-beta")
    write(source / "next-login-home", str(selected))
    home = prepare(source)
    replace_auth(home, credential(refresh="2026-03-01T00:00:00Z", token="keep-private"))
    write(home / "current", "login-alpha\n")
    before, system = tree(home), tree(source)
    original_bind = auth._bind_selected_auth_pointer
    original_remove = auth.shutil.rmtree
    failed = False
    def bind(*args):
        if failure == "after-binding":
            original_bind(*args)
        if failure in {"binding", "after-binding"}:
            raise OSError("fixture bind refusal")
        return original_bind(*args)
    def remove(path, *args, **kwargs):
        nonlocal failed
        if failure == "partial-cleanup" and Path(path).name == ".termdeck-accounts-migration" and not failed:
            failed = True
            (Path(path) / "login-beta.json").unlink()
            raise OSError("fixture partial cleanup refusal")
        return original_remove(path, *args, **kwargs)
    monkeypatch.setattr(auth, "_bind_selected_auth_pointer", bind)
    monkeypatch.setattr(auth.shutil, "rmtree", remove)
    with pytest.raises(auth.CodexAuthError, match="previous selection restored"):
        auth.CodexAuthManager(account_source=source).migrate_catalog(record(home))
    assert tree(home) == before and tree(source) == system


def test_source_change_during_private_publication_is_refused_without_losing_refresh(source, monkeypatch):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    home = prepare(source)
    replace_auth(home, credential(refresh="2026-03-01T00:00:00Z", token="keep-private"))
    before = tree(home)
    original = auth._restore_private_preimage
    changed = False
    def stage(path, preimage):
        nonlocal changed
        original(path, preimage)
        if not changed and path.parent.name.startswith(".termdeck-profile-catalogue-"):
            changed = True
            write(selected / "auth.json", credential(refresh="2026-04-01T00:00:00Z", token="concurrent-source"))
    monkeypatch.setattr(auth, "_restore_private_preimage", stage)
    with pytest.raises(auth.CodexAuthError, match="changed"):
        auth.CodexAuthManager(account_source=source).migrate_catalog(record(home))
    assert tree(home) == before


def test_profile_directory_replacement_during_read_is_refused(source, monkeypatch):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    original = catalogue.read
    changed = False
    def rotating(path, **kwargs):
        nonlocal changed
        result = original(path, **kwargs)
        if not changed:
            changed = True
            selected.rename(source / "previous-profile")
            selected.symlink_to(source / "previous-profile", target_is_directory=True)
        return result
    monkeypatch.setattr(catalogue, "read", rotating)
    with pytest.raises(auth.CodexAuthError, match="changed"):
        auth.read_auth_state(source.parent / "tab", account_source=source)


def test_private_history_stays_private_across_account_migration(source):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    for name in ("sessions", "archived_sessions"):
        (source / name).mkdir(mode=0o700)
        write(source / name / "sentinel", "system-history")
    before = tree(source)
    home = prepare(source, private_history=True)
    auth.CodexAuthManager(account_source=source).migrate_catalog(record(home))
    for name in ("sessions", "archived_sessions"):
        assert (home / name).is_dir() and not (home / name).is_symlink()
        assert list((home / name).iterdir()) == []
    assert tree(source) == before


def test_private_history_refuses_existing_shared_history(source):
    profile(source, "login-alpha")
    (source / "sessions").mkdir(mode=0o700)
    home = source.parent / "tab"
    home.mkdir(mode=0o700)
    (home / "sessions").symlink_to(source / "sessions", target_is_directory=True)
    before = tree(source)
    with pytest.raises(auth.CodexAuthError, match="direct directory"):
        prepare(source, private_history=True)
    assert tree(source) == before and (home / "sessions").is_symlink()


def test_system_source_cannot_be_a_private_scope(source):
    profile(source, "login-alpha")
    before = tree(source)
    for destination in (source, source / "login-homes" / "login-alpha", source / "new-private"):
        with pytest.raises(auth.CodexAuthError, match="cannot contain"):
            auth.prepare_auth_scope(destination, account_source=source)
    assert tree(source) == before


def test_explicit_root_alias_is_resolved_without_following_descendant_profile_links(source):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    alias = source.parent / "user-codex"
    alias.symlink_to(source, target_is_directory=True)
    before = tree(source)
    home = auth.prepare_auth_scope(source.parent / "tab", account_source=alias)
    assert auth.read_auth_state(home, account_source=alias).selected_account_key == "login-alpha"
    assert tree(source) == before


def test_source_owner_and_catalogue_size_are_checked(source, monkeypatch):
    profile(source, "login-alpha")
    profile(source, "login-beta")
    monkeypatch.setattr(catalogue, "_LIMIT", 1)
    with pytest.raises(auth.CodexAuthError, match="entry limit"):
        auth.read_auth_state(source.parent / "tab", account_source=source)
    monkeypatch.setattr(catalogue, "_LIMIT", 1024)
    actual_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(auth.CodexAuthError, match="owned"):
        auth.read_auth_state(source.parent / "tab", account_source=source)


@pytest.mark.parametrize("malformed", [False, True])
def test_private_registry_must_be_direct_valid_metadata(source, malformed):
    selected = profile(source, "login-alpha")
    write(source / "next-login-home", str(selected))
    home = prepare(source)
    path = home / "accounts" / "registry.json"
    if malformed:
        write(path, "[]")
    else:
        path.unlink()
        path.symlink_to(source / "next-login-home")
    before = tree(source)
    with pytest.raises(auth.CodexAuthError):
        auth.read_auth_state(home, account_source=source)
    assert tree(source) == before


def test_native_alias_replacement_preserves_archive_and_selected_identity(source):
    legacy = saved(source, "24", credential("previous", token="legacy-private-fixture"))
    previous = profile(source, "login-previous", credential("previous"))
    selected = profile(source, "login-selected", credential("new"))
    other = profile(source, "login-other", credential("other"))
    (source / "auth.json").symlink_to(legacy)
    write(source / "current", "24\n")
    write(source / "next-login-home", str(selected))
    write(source / "account-profiles.json", {
        "24": str(selected), "24.previous-fixture": str(previous), "other": str(other),
    })
    before = tree(source)
    state = auth.read_auth_state(prepare(source), account_source=source)
    selected_row = auth.resolve_account(state, "24")
    assert selected_row.account_key == state.selected_account_key == state.canonical_account_key == "24"
    assert read(Path(state.codex_home) / "accounts" / "24.json").identity == read(selected / "auth.json").identity
    assert auth.resolve_account(state, "login-selected").account_key == "24"
    old_row = auth.resolve_account(state, "24.previous-fixture")
    assert old_row.account_key == auth.resolve_account(state, "login-previous").account_key != "24"
    assert read(Path(state.codex_home) / "accounts" / (old_row.account_key + ".json")).identity == read(previous / "auth.json").identity
    rows = {item["account_key"]: set(item["selectors"])
            for item in auth._source_account_catalog(source).registry["accounts"]}
    assert rows["24"] == {"24", "login-selected"}
    assert rows[old_row.account_key] == {"24.previous-fixture", "login-previous"}
    assert all(not names & rows["24"] for key, names in rows.items() if key != "24")
    with pytest.raises(auth.CodexAuthError):
        auth.resolve_account(state, "arbitrary-not-an-account")
    assert state.credential_status == state.canonical_credential_status == "current"
    assert tree(source) == before


@pytest.mark.parametrize("damage", [
    "missing-home", "missing-auth", "malformed-auth", "foreign-home",
    "relative-home", "profile-link", "credential-mode", "alias-type",
])
def test_authoritative_alias_damage_never_falls_back_to_valid_legacy(source, damage):
    saved(source, "24", credential("previous"))
    selected = profile(source, "login-selected", credential("new"))
    target = str(selected)
    if damage == "missing-home":
        target = str(selected.parent / "login-missing")
    elif damage == "missing-auth":
        (selected / "auth.json").unlink()
    elif damage == "malformed-auth":
        write(selected / "auth.json", {"tokens": {"account_id": "new"}})
    elif damage == "foreign-home":
        outside = source.parent / "foreign"
        outside.mkdir(mode=0o700)
        write(outside / "auth.json", credential("new"))
        target = str(outside)
    elif damage == "relative-home":
        target = "login-homes/login-selected"
    elif damage == "profile-link":
        selected.rename(source / "held")
        selected.symlink_to(source / "held", target_is_directory=True)
    elif damage == "credential-mode":
        (selected / "auth.json").chmod(0o666)
    else:
        target = 24
    write(source / "account-profiles.json", {"24": target})
    before = tree(source)
    with pytest.raises(auth.CodexAuthError):
        auth.read_auth_state(source.parent / "uncreated-tab", account_source=source)
    assert tree(source) == before
    assert not (source.parent / "uncreated-tab").exists()


def test_authoritative_alias_different_case_cannot_shadow_legacy_selector(source):
    saved(source, "Work", credential("previous"))
    selected = profile(source, "login-selected", credential("new"))
    write(source / "account-profiles.json", {"work": str(selected)})
    before = tree(source)
    with pytest.raises(auth.CodexAuthError, match="differ only by case"):
        auth.read_auth_state(source.parent / "uncreated-tab", account_source=source)
    assert tree(source) == before


@pytest.mark.parametrize("newer", ["legacy", "profile", "equal"])
def test_alias_same_principal_retains_generation_order_and_stale_selection(source, newer):
    legacy_value = credential("same")
    profile_value = credential("same")
    if newer == "legacy":
        legacy_value = credential("same", refresh="2026-02-01T00:00:00Z", token="new-legacy")
    elif newer == "profile":
        profile_value = credential("same", refresh="2026-02-01T00:00:00Z", token="new-profile")
    legacy = saved(source, "24", legacy_value)
    selected = profile(source, "login-selected", profile_value)
    write(source / "account-profiles.json", {"24": str(selected)})
    write(source / "next-login-home", str(selected))
    before = tree(source)
    system = auth._source_account_catalog(source)
    assert system.canonical.key == "24"
    expected = legacy if newer == "legacy" else selected / "auth.json"
    assert system.credentials["24"] == expected
    assert system.canonical.status == ("stale" if newer == "legacy" else "current")
    assert set(system.alternate_credentials["24"]) == {legacy, selected / "auth.json"}
    assert tree(source) == before


def test_alias_replacement_cannot_reuse_previous_principal_private_copy(source):
    legacy = saved(source, "24", credential("previous"))
    previous = profile(source, "login-previous", credential("previous"))
    (source / "auth.json").symlink_to(legacy)
    write(source / "account-profiles.json", {"24": str(previous)})
    write(source / "next-login-home", str(previous))
    home = prepare(source)
    selected = profile(source, "login-selected", credential("new"))
    write(source / "account-profiles.json", {
        "24": str(selected), "24.previous-fixture": str(previous),
    })
    write(source / "next-login-home", str(selected))
    before, private = tree(source), tree(home)
    with pytest.raises(auth.CodexAuthError, match="conflicts with its current system identity"):
        auth.read_auth_state(home, account_source=source)
    assert tree(source) == before and tree(home) == private


def test_alias_credential_replacement_during_validation_is_refused(source, monkeypatch):
    saved(source, "24", credential("previous"))
    selected = profile(source, "login-selected", credential("new"))
    write(source / "account-profiles.json", {"24": str(selected)})
    original = catalogue.read
    changed = False

    def replace_after_read(path, **kwargs):
        nonlocal changed
        result = original(path, **kwargs)
        if not changed:
            changed = True
            replace_auth(selected, credential("new", token="concurrent-fixture"))
        return result

    monkeypatch.setattr(catalogue, "read", replace_after_read)
    with pytest.raises(auth.CodexAuthError, match="changed during observation"):
        auth.read_auth_state(source.parent / "uncreated-tab", account_source=source)
    assert changed
    assert not (source.parent / "uncreated-tab").exists()
