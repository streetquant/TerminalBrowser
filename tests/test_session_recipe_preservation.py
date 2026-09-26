import sqlite3

import pytest

from termdeck.session_recipe import (
    SessionRecipeError,
    build_session_recipe,
    encode_session_recipe,
    recipe_digest,
    recipe_from_record,
)
from termdeck.session_store import SessionStore


@pytest.fixture
def store():
    registry = SessionStore(":memory:", provision_auth_scopes=False)
    try:
        yield registry
    finally:
        registry.close()


def canonical_record(store, tmp_path, *, name="saved"):
    record = store.create(
        name, "recipe-test", "original-launcher", str(tmp_path),
        window_id="saved-window", window_title="Saved", tab_index=7,
        visible=False, pinned=True,
    )
    recipe = build_session_recipe(
        "worker --resume canonical-job", tmp_path / "canonical",
        environment={"MODE": "durable", "LABEL": "two words"},
        origin="native-resume",
    )
    encoded, digest = encode_session_recipe(recipe)
    return store.update(
        record.id, status="running", recovery_recipe=encoded,
        recovery_recipe_sha256=digest,
    )


@pytest.mark.parametrize("observation", ["unchanged", "weaker", "external"])
def test_changed_batch_preserves_unaffected_canonical_record(store, tmp_path, observation):
    saved = canonical_record(store, tmp_path)
    command, cwd = None, None
    if observation == "weaker":
        saved = store.update(
            saved.id, recovery_command="worker --resume saved-job",
            recovery_cwd=str(tmp_path / "saved-job"),
        )
        command, cwd = "worker", str(tmp_path / "weaker")
    elif observation == "external":
        saved = store.update(saved.id, external=True)
        command, cwd = "worker --resume external-job", str(tmp_path)
    neighbor = store.create("neighbor", "recipe-test", "worker", str(tmp_path))

    changed = store.update_recovery_metadata([
        (saved.id, command, cwd),
        (neighbor.id, "worker --resume observed-job", str(tmp_path)),
        ("missing-id", "worker", str(tmp_path)),
    ])

    assert changed == 1
    assert store.get(saved.id) == saved
    assert recipe_from_record(store.get(neighbor.id)).command == "worker --resume observed-job"


@pytest.mark.parametrize("field", [
    "command", "cwd", "recovery_command", "recovery_cwd", "restart_policy",
])
def test_intended_update_preserves_environment_and_other_canonical_fields(store, tmp_path, field):
    saved = canonical_record(store, tmp_path)
    before = recipe_from_record(saved)
    values = {
        "command": "replacement --foreground",
        "cwd": str(tmp_path / "new-cwd"),
        "recovery_command": "worker --resume observed-job",
        "recovery_cwd": str(tmp_path / "foreground"),
        "restart_policy": "always",
    }

    updated = store.update(saved.id, **{field: values[field]})
    recipe = recipe_from_record(updated)

    assert recipe.environment_dict() == before.environment_dict()
    assert recipe.command == (values[field] if field in {"command", "recovery_command"} else before.command)
    assert recipe.cwd == (values[field] if field in {"cwd", "recovery_cwd"} else before.cwd)
    assert recipe.restart_policy == (values[field] if field == "restart_policy" else before.restart_policy)
    for key in ("window_id", "window_title", "tab_index", "visible", "pinned", "status"):
        assert getattr(updated, key) == getattr(saved, key)


def test_repeated_legacy_inputs_do_not_replace_canonical_authority(store, tmp_path):
    saved = canonical_record(store, tmp_path)

    updated = store.update(
        saved.id, command=saved.command, cwd=saved.cwd,
        recovery_command=saved.recovery_command, recovery_cwd=saved.recovery_cwd,
        restart_policy=saved.restart_policy,
    )

    assert updated.recovery_recipe == saved.recovery_recipe
    assert updated.recovery_recipe_sha256 == saved.recovery_recipe_sha256


def test_batch_observation_and_clear_keep_explicit_environment(store, tmp_path):
    saved = canonical_record(store, tmp_path)
    before = recipe_from_record(saved)
    observed_cwd = str(tmp_path / "foreground")

    assert store.update_recovery_metadata([
        (saved.id, "worker --resume observed-job", observed_cwd),
    ]) == 1
    observed = store.get(saved.id)
    recipe = recipe_from_record(observed)
    assert recipe.command == "worker --resume observed-job"
    assert recipe.cwd == observed_cwd
    assert recipe.environment == before.environment
    assert observed.last_opened_at == saved.last_opened_at

    assert store.update_recovery_metadata([(saved.id, None, None)]) == 1
    cleared = recipe_from_record(store.get(saved.id))
    assert cleared.command == saved.command
    assert cleared.cwd == saved.cwd
    assert cleared.environment == before.environment


def corrupt_receipt(store, saved, corruption):
    encoded, digest = saved.recovery_recipe, saved.recovery_recipe_sha256
    if corruption == "wrong-digest":
        digest = "0" * 64
    elif corruption == "missing-digest":
        digest = None
    elif corruption == "missing-recipe":
        encoded = None
    else:
        encoded = "{}"
        digest = recipe_digest(encoded)
    store.conn.execute(
        "UPDATE sessions SET recovery_recipe = ?, recovery_recipe_sha256 = ? WHERE id = ?",
        (encoded, digest, saved.id),
    )
    store.conn.commit()
    return store.get(saved.id)


@pytest.mark.parametrize("corruption", [
    "wrong-digest", "missing-digest", "missing-recipe", "invalid-fields",
])
def test_ordinary_update_rejects_invalid_saved_recipe(store, tmp_path, corruption):
    saved = corrupt_receipt(store, canonical_record(store, tmp_path), corruption)

    with pytest.raises(SessionRecipeError):
        store.update(saved.id, cwd=str(tmp_path / "updated"))

    assert store.get(saved.id) == saved
    assert not store.conn.in_transaction


@pytest.mark.parametrize("corruption", [
    "wrong-digest", "missing-digest", "missing-recipe", "invalid-fields",
])
def test_invalid_changed_batch_member_cannot_partially_commit(store, tmp_path, corruption):
    neighbor = canonical_record(store, tmp_path, name="neighbor")
    saved = corrupt_receipt(store, canonical_record(store, tmp_path), corruption)

    with pytest.raises(SessionRecipeError):
        store.update_recovery_metadata([
            (neighbor.id, "worker --resume neighbor-job", str(tmp_path)),
            (saved.id, "worker --resume invalid-job", str(tmp_path)),
        ])

    assert store.get(neighbor.id) == neighbor
    assert store.get(saved.id) == saved
    assert not store.conn.in_transaction
    store.remember_window("later-write", "Later write")
    assert store.get(neighbor.id) == neighbor
    assert store.get(saved.id) == saved


def test_unchanged_invalid_receipt_is_preserved_without_blocking_other_updates(store, tmp_path):
    saved = corrupt_receipt(store, canonical_record(store, tmp_path), "wrong-digest")
    neighbor = canonical_record(store, tmp_path, name="neighbor")

    assert store.update_recovery_metadata([
        (saved.id, None, None),
        (neighbor.id, "worker --resume neighbor-job", str(tmp_path)),
    ]) == 1

    assert store.get(saved.id) == saved
    with pytest.raises(SessionRecipeError):
        recipe_from_record(store.get(saved.id))


def test_batch_sql_failure_rolls_back_metadata_and_recipe_together(store, tmp_path):
    first = canonical_record(store, tmp_path, name="first")
    second = canonical_record(store, tmp_path, name="second")
    store.conn.execute("""
        CREATE TEMP TRIGGER reject_second BEFORE UPDATE ON sessions
        WHEN OLD.name = 'second'
        BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END
    """)
    store.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="fixture write failure"):
        store.update_recovery_metadata([
            (first.id, "worker --resume first-job", str(tmp_path)),
            (second.id, "worker --resume second-job", str(tmp_path)),
        ])

    assert store.get(first.id) == first
    assert store.get(second.id) == second
    assert not store.conn.in_transaction


def test_legacy_record_without_receipt_still_accepts_observed_recovery(store, tmp_path):
    saved = canonical_record(store, tmp_path)
    store.conn.execute(
        "UPDATE sessions SET recovery_recipe = NULL, recovery_recipe_sha256 = NULL WHERE id = ?",
        (saved.id,),
    )
    store.conn.commit()

    assert store.update_recovery_metadata([
        (saved.id, "worker --resume legacy-job", str(tmp_path)),
    ]) == 1

    recipe = recipe_from_record(store.get(saved.id))
    assert recipe.command == "worker --resume legacy-job"
    assert recipe.cwd == str(tmp_path)


def test_explicit_valid_recipe_replacement_remains_authoritative(store, tmp_path):
    saved = corrupt_receipt(store, canonical_record(store, tmp_path), "wrong-digest")
    replacement = build_session_recipe(
        "replacement --resume chosen-job", tmp_path / "chosen",
        environment={"MODE": "chosen"}, restart_policy="manual",
    )
    encoded, digest = encode_session_recipe(replacement)

    updated = store.update(
        saved.id, command="legacy-value", cwd=str(tmp_path / "legacy"),
        recovery_recipe=encoded, recovery_recipe_sha256=digest,
    )

    assert recipe_from_record(updated) == replacement
    assert updated.restart_policy == "manual"


def test_repeated_observations_in_one_batch_do_not_weaken_resume_metadata(store, tmp_path):
    saved = canonical_record(store, tmp_path)
    observed_cwd = str(tmp_path / "observed")

    assert store.update_recovery_metadata([
        (saved.id, "worker --resume chosen-job", observed_cwd),
        (saved.id, "worker", str(tmp_path / "weaker")),
    ]) == 1

    recipe = recipe_from_record(store.get(saved.id))
    assert recipe.command == "worker --resume chosen-job"
    assert recipe.cwd == observed_cwd
    assert recipe.environment == recipe_from_record(saved).environment


def test_recovery_launch_replays_preserved_recipe_environment(store, tmp_path):
    from termdeck.tmux_backend import TmuxBackend

    class NoCheckpoint:
        def restart_command(self, _record):
            return None

        def wrap_launch_command(self, _record, command):
            return command

    saved = canonical_record(store, tmp_path)
    updated_cwd = tmp_path / "updated"
    updated_cwd.mkdir()
    store.update(saved.id, cwd=str(updated_cwd))
    store.update_recovery_metadata([
        (saved.id, "worker --resume observed-job", str(updated_cwd)),
    ])
    backend = TmuxBackend(executable="/usr/bin/tmux", checkpoint_manager=NoCheckpoint())

    spec = backend._launch_spec(store.get(saved.id))

    assert "worker --resume observed-job" in spec.command
    assert spec.directory == updated_cwd
    assert dict(spec.environment)["MODE"] == "durable"
    assert dict(spec.environment)["LABEL"] == "two words"
