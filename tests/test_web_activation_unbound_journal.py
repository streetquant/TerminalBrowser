"""Recovery of failures before an inner journal acquired outer ownership."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from termdeck import release_manager
from termdeck import web_activation as activation


def _capture(path: Path):
    return activation._capture_anchored(path, label="disposable recovery fixture")[1]


def _fixture(tmp_path: Path, *, content: bytes | None = b"original A", journal_bytes: bytes = b""):
    target = tmp_path / "runtime" / "backend"
    target.parent.mkdir()
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    if content is not None:
        target.write_bytes(content)
        target.chmod(0o755)
    record = activation._preimage(target, tx, "backend")
    _intent, journal = activation._publication_intent(record, target, tx, b"candidate B", 0o755)
    with journal.open("xb") as output:
        output.write(journal_bytes)
    journal.chmod(0o600)
    return target, tx, record, journal


@pytest.mark.parametrize("content", [b"original A", None], ids=["existing", "originally-absent"])
@pytest.mark.parametrize("journal_bytes", [b"", b"opaque unowned journal"], ids=["empty", "opaque"])
def test_unbound_journal_leaves_exact_preimage_and_journal_untouched(
    tmp_path, monkeypatch, content, journal_bytes,
):
    target, tx, record, journal = _fixture(tmp_path, content=content, journal_bytes=journal_bytes)
    original = _capture(target)
    journal_before = _capture(journal)
    record_before = copy.deepcopy(record)
    monkeypatch.setattr(
        release_manager, "recover_publication_transaction",
        lambda *_args, **_kwargs: pytest.fail("an unbound journal was consumed"),
    )

    for _ in range(2):
        assert activation._restore(
            record, tx, checkpoint=lambda: pytest.fail("unbound ownership was checkpointed"),
        ) == "already-restored"
        assert _capture(target) == original
        assert _capture(journal) == journal_before
        assert record == record_before


def test_failed_real_binary_journal_initialization_can_complete_outer_inverse(tmp_path, monkeypatch):
    """The live failure's actual byte counts run through the real publication API."""

    target = tmp_path / "runtime" / "backend"
    target.parent.mkdir()
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    target.write_bytes(b"A" * 5_219_640)
    target.chmod(0o755)
    record = activation._preimage(target, tx, "backend")
    original = _capture(target)
    journal = activation._publication_journal_path(target, tx)
    real_persist = release_manager._PublicationJournal.persist

    def fail_initial_journal(self, *, state=None):
        if self.path == journal:
            raise release_manager.ReleaseError("injected initial journal write failure")
        return real_persist(self, state=state)

    with monkeypatch.context() as failure:
        failure.setattr(release_manager._PublicationJournal, "persist", fail_initial_journal)
        with pytest.raises(activation.WebActivationError, match="initialization was not durably resolved"):
            activation._publish_preimaged(
                record, tx, b"B" * 5_847_728, 0o755, label="disposable real-size binary",
                intent_checkpoint=lambda: activation._write(tx / "record.json", record),
            )
    persisted = activation._read(tx / "record.json")
    assert persisted == record
    assert record["publication_intent"]["state"] == "prepared"
    assert record["publication_intent"]["journal_identity"] is None
    assert record["postimage"] is None
    journal_before = _capture(journal)
    assert journal_before.snapshot.exists
    assert _capture(target) == original
    assert activation._restore(persisted, tx) == "already-restored"
    assert activation._restore(persisted, tx) == "already-restored"
    assert _capture(target) == original
    assert _capture(journal) == journal_before


def test_unbound_journal_recovery_repeats_after_process_restart(tmp_path):
    target, tx, record, journal = _fixture(tmp_path)
    record_path = tx / "record.json"
    activation._write(record_path, record)
    before = [_capture(path) for path in (target, journal, record_path)]
    program = """
import json, sys
from pathlib import Path
from termdeck import release_manager, web_activation as activation
def forbidden(*args, **kwargs):
    raise AssertionError('unbound journal recovery is forbidden')
release_manager.recover_publication_transaction = forbidden
path = Path(sys.argv[1])
record = activation._read(path)
print(json.dumps([activation._restore(record, path.parent) for _ in range(2)]))
"""
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", program, str(record_path)],
        cwd=project, env={**os.environ, "PYTHONPATH": str(project)},
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["already-restored", "already-restored"]
    assert [_capture(path) for path in (target, journal, record_path)] == before


@pytest.mark.parametrize("change", [
    "same-bytes-foreign-inode", "foreign-b", "in-place-bytes", "mode", "mtime", "ctime-only",
    "missing", "symlink", "hardlink", "parent-replaced", "parent-symlink",
    "backup-replaced", "backup-symlink", "preimage-identity", "postimage",
    "malformed-postimage", "rollback-postimage", "rollback-publication",
    "prepared-bound-identity", "journal-intent-without-identity", "journal-symlink", "journal-hardlink",
])
def test_unbound_journal_does_not_authorize_changed_or_contradictory_preimage(tmp_path, change):
    target, tx, record, journal = _fixture(tmp_path)
    original_journal = journal
    journal_before = _capture(journal)
    original = _capture(target)
    if change in {"same-bytes-foreign-inode", "foreign-b"}:
        replacement = target.with_name("foreign")
        replacement.write_bytes(original.snapshot.content if change.startswith("same-") else b"candidate B")
        replacement.chmod(0o755)
        replacement.replace(target)
    elif change == "in-place-bytes":
        target.write_bytes(b"changed A!")
    elif change == "mode":
        target.chmod(0o700)
    elif change == "mtime":
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    elif change == "ctime-only":
        target.chmod(0o700)
        target.chmod(0o755)
        assert _capture(target).identity.ctime_ns != original.identity.ctime_ns
        assert activation._same_preimage_inode(_capture(target), original)
    elif change == "missing":
        target.unlink()
    elif change == "symlink":
        saved = target.with_name("saved")
        target.rename(saved)
        target.symlink_to(saved)
    elif change == "hardlink":
        os.link(target, target.with_name("linked"))
    elif change.startswith("parent-"):
        old_parent = target.parent.with_name("saved-runtime")
        target.parent.rename(old_parent)
        original_journal = old_parent / journal.name
        if change == "parent-symlink":
            target.parent.symlink_to(old_parent, target_is_directory=True)
        else:
            target.parent.mkdir()
            (old_parent / target.name).rename(target)
            (old_parent / journal.name).rename(journal)
            original_journal = journal
        journal_before = _capture(original_journal)
    elif change.startswith("backup-"):
        backup = tx / record["backup"]
        saved = backup.with_name("foreign-backup")
        saved.write_bytes(backup.read_bytes())
        saved.chmod(backup.stat().st_mode & 0o7777)
        if change == "backup-replaced":
            saved.replace(backup)
        else:
            backup.unlink()
            backup.symlink_to(saved)
    elif change == "preimage-identity":
        record["preimage_identity"]["inode"] += 1
    elif change == "postimage":
        guards = activation._record_parent_guards(record, target)
        record["postimage"] = activation._postimage_payload(target, original, guards)
        for field in ("sha256", "size", "mode"):
            record["publication_intent"][field] = record["postimage"][field]
    elif change == "malformed-postimage":
        record["postimage"] = "not an owned postimage"
    elif change == "rollback-postimage":
        record["rollback_postimage"] = {}
    elif change == "rollback-publication":
        record["rollback_publication"] = {}
    elif change == "prepared-bound-identity":
        record["publication_intent"]["journal_identity"] = activation._file_identity_payload(journal_before.identity)
    elif change == "journal-intent-without-identity":
        record["publication_intent"]["state"] = "journal-intent"
    elif change == "journal-symlink":
        original_journal = journal.with_name("foreign-journal")
        journal.rename(original_journal)
        journal.symlink_to(original_journal)
        journal_before = _capture(original_journal)
    elif change == "journal-hardlink":
        os.link(journal, journal.with_name("linked-journal"))
    else:
        raise AssertionError(change)

    record_before = copy.deepcopy(record)
    journal_stat = original_journal.stat()
    journal_content = original_journal.read_bytes()
    with pytest.raises(activation.WebActivationError):
        activation._restore(record, tx)
    assert record == record_before
    after = original_journal.stat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns) == (
        journal_stat.st_dev, journal_stat.st_ino, journal_stat.st_mode, journal_stat.st_nlink,
        journal_stat.st_size, journal_stat.st_mtime_ns, journal_stat.st_ctime_ns,
    )
    assert original_journal.read_bytes() == journal_content


def test_unpublished_preimage_is_revalidated_at_outer_restore_read(tmp_path, monkeypatch):
    target, tx, record, journal = _fixture(tmp_path)
    journal_before = _capture(journal)
    real_capture = activation._capture_anchored

    def change_before_outer_read(path, *, label, **kwargs):
        if path == target and label == "activation rollback target":
            target.chmod(0o700)
            target.chmod(0o755)
        return real_capture(path, label=label, **kwargs)

    monkeypatch.setattr(activation, "_capture_anchored", change_before_outer_read)
    with pytest.raises(activation.WebActivationError, match="changed"):
        activation._restore(record, tx)
    assert _capture(journal) == journal_before


def test_unbound_inverse_journal_does_not_claim_b_was_restored(tmp_path):
    target, tx, record, forward = _fixture(tmp_path)
    # Fixture setup removes its own empty placeholder before a real forward publication.
    forward.unlink()
    activation._publish_preimaged(record, tx, b"candidate B", 0o755, label="owned B fixture")
    inverse = activation._rollback_publication_journal_path(target, tx)
    inverse.touch(mode=0o600)
    before = [_capture(path) for path in (target, inverse)]
    assert record.get("rollback_publication") is None
    for _ in range(2):
        with pytest.raises(activation.WebActivationError):
            activation._restore(record, tx)
        assert [_capture(path) for path in (target, inverse)] == before
        assert record.get("rollback_publication") is None


def test_outer_rollback_completes_and_repeats_without_touching_unbound_journal(tmp_path, monkeypatch):
    paths = activation.ActivationPaths(
        target=tmp_path / "source", evidence=tmp_path / "recovery" / "activation-v2",
        runtime=tmp_path / "runtime", database=tmp_path / "sessions.sqlite3",
        managed_root=tmp_path / "managed", managed_bin=tmp_path / "bin", unit_dir=tmp_path / "units",
    )
    tx, receipt = activation.new_transaction(
        paths, source={"commit": "a" * 40, "tree": "b" * 40, "branch": "fixture", "origin_main": "a" * 40},
    )
    target = paths.runtime / "backend"
    target.parent.mkdir()
    target.write_bytes(b"original A")
    target.chmod(0o755)
    record = activation._preimage(target, tx, "backend")
    _intent, journal = activation._publication_intent(record, target, tx, b"candidate B", 0o755)
    journal.touch(mode=0o600)
    original = _capture(target)
    journal_before = _capture(journal)
    release_a = "a" * 64
    activation._write(paths.managed_root / "state.json", {"current_release_id": release_a, "generation": 1})
    receipt.update(
        mutation_started=True, preimages={"runtime:backend": record}, old_release_id=release_a,
        database_before={"session_ids": []}, processes_before=[], services_stopped=False,
        services_may_be_live=False, watchdog={"unit": activation.WATCHDOG_UNIT},
    )
    activation._persist(paths, tx, receipt, "rollback-failed")
    activation._atomic(paths.recovery_required, (str(tx) + "\n").encode(), 0o600)
    actions = []
    monkeypatch.setattr(activation, "_systemd", lambda *args, **kwargs: actions.append(args) or SimpleNamespace(returncode=0))
    for method in ("_stop_web", "_start_web", "_disarm"):
        monkeypatch.setattr(activation, method, lambda: pytest.fail("unexpected service action"))
    monkeypatch.setattr(activation, "verify_database", lambda *_args: {"integrity": "ok", "lost_session_ids": []})
    monkeypatch.setattr(activation, "process_observation", lambda *_args: {"disappeared": [], "natural_exit_allowed": True})

    assert activation.rollback(paths, tx, reason="journal failed before publication")["action"] == "rolled-back"
    final = activation._read(tx / "receipt.json")
    final_record = final["preimages"]["runtime:backend"]
    assert final_record["rollback_restore"] == "already-restored"
    assert final_record["publication_intent"] == record["publication_intent"]
    assert final_record["postimage"] is None
    assert not paths.recovery_required.exists()
    assert actions == [("daemon-reload",)]  # Recorded fixture call, never a real systemd action.
    final_receipt = _capture(tx / "receipt.json")
    assert activation.rollback(paths, tx, reason="repeat inverse")["action"] == "rolled-back"
    assert _capture(tx / "receipt.json") == final_receipt
    assert actions == [("daemon-reload",)]
    assert _capture(target) == original
    assert _capture(journal) == journal_before


class _InverseInterrupted(BaseException):
    pass


def _owned_b_fixture(tmp_path):
    target, tx, record, journal = _fixture(tmp_path)
    journal.unlink()  # Discard only the fixture's own pre-publication placeholder.
    record_path = tx / "record.json"
    activation._publish_preimaged(
        record, tx, b"candidate B", 0o755, label="owned inverse B fixture",
        intent_checkpoint=lambda: activation._write(record_path, record),
        checkpoint=lambda: activation._write(record_path, record),
    )
    return target, tx, record, record_path


def _restore_in_new_process(record_path):
    program = """
import json, sys
from pathlib import Path
from termdeck import web_activation as a
path = Path(sys.argv[1]); record = a._read(path)
def checkpoint():
    a._write(path, record)
print(json.dumps([a._restore(record, path.parent, checkpoint=checkpoint) for _ in range(2)]))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(record_path)], cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["restored", "already-restored"]


@pytest.mark.parametrize("phase", ["intent", "published"])
@pytest.mark.parametrize("failure", [RuntimeError, _InverseInterrupted], ids=["exception", "cancellation"])
def test_local_inverse_compensation_hands_off_exact_b_before_retiring_journal(tmp_path, phase, failure):
    target, tx, record, record_path = _owned_b_fixture(tmp_path)
    journal = activation._rollback_publication_journal_path(target, tx)
    handed_off = []

    def checkpoint():
        activation._write(record_path, record)
        value = record.get("rollback_publication", {})
        if value.get("state") == phase:
            raise failure("injected inverse checkpoint interruption")
        if value.get("state") == "recovered":
            assert journal.exists()
            current = _capture(target)
            activation._expected_postimage(target, current, record["postimage"])
            assert current.snapshot.content == b"candidate B"
            handed_off.append(copy.deepcopy(value))

    with pytest.raises(activation.WebActivationError if failure is RuntimeError else failure):
        activation._restore(record, tx, checkpoint=checkpoint)
    durable = activation._read(record_path)
    assert handed_off and durable["rollback_publication"] == handed_off[-1]
    assert durable["rollback_publication"]["state"] == "recovered"
    assert "rollback_postimage" not in durable
    assert not journal.exists()
    _restore_in_new_process(record_path)
    assert target.read_bytes() == b"original A"


@pytest.mark.parametrize("checkpoint_completed", [False, True], ids=["before-checkpoint", "after-checkpoint"])
def test_interrupted_local_inverse_recovery_handoff_retains_retryable_journal(tmp_path, checkpoint_completed):
    target, tx, record, record_path = _owned_b_fixture(tmp_path)
    journal = activation._rollback_publication_journal_path(target, tx)
    reached_recovery = []

    def checkpoint():
        phase = record.get("rollback_publication", {}).get("state")
        if phase == "recovered":
            reached_recovery.append(True)
            if checkpoint_completed:
                activation._write(record_path, record)
            raise _InverseInterrupted("recovery handoff interrupted")
        activation._write(record_path, record)
        if phase == "published":
            raise RuntimeError("original inverse checkpoint failed")

    with pytest.raises((activation.WebActivationError, _InverseInterrupted)):
        activation._restore(record, tx, checkpoint=checkpoint)
    assert reached_recovery
    assert journal.exists()
    assert target.read_bytes() == b"candidate B"
    durable = activation._read(record_path)
    assert durable["rollback_publication"]["state"] == ("recovered" if checkpoint_completed else "published")
    _restore_in_new_process(record_path)
    assert target.read_bytes() == b"original A"
    assert not journal.exists()


def test_local_inverse_compensation_does_not_adopt_same_bytes_foreign_b(tmp_path):
    target, tx, record, record_path = _owned_b_fixture(tmp_path)

    def checkpoint():
        activation._write(record_path, record)
        if record.get("rollback_publication", {}).get("state") == "published":
            raise _InverseInterrupted()

    with pytest.raises(_InverseInterrupted):
        activation._restore(record, tx, checkpoint=checkpoint)
    assert activation._read(record_path)["rollback_publication"]["state"] == "recovered"
    foreign = target.with_name("foreign-b")
    foreign.write_bytes(b"candidate B")
    foreign.chmod(0o755)
    foreign.replace(target)
    replacement = _capture(target)
    durable = activation._read(record_path)
    for _ in range(2):
        with pytest.raises(activation.WebActivationError, match="changed before rollback"):
            activation._restore(durable, tx)
        assert _capture(target) == replacement


@pytest.mark.parametrize("changed_binding", ["journal-path", "journal-inode", "parents", "preimage"])
def test_local_inverse_recovery_handoff_rejects_changed_callback_binding(tmp_path, monkeypatch, changed_binding):
    target, tx, record, record_path = _owned_b_fixture(tmp_path)
    journal = activation._rollback_publication_journal_path(target, tx)
    publish = release_manager._publish_files_transactionally
    intercepted = []

    def corrupted_recovery(writes, **kwargs):
        callback = kwargs.get("after_recovery")
        if kwargs.get("journal_path") == journal and callback is not None:
            def changed(publication):
                intercepted.append(True)
                if changed_binding == "journal-path":
                    publication = dataclasses.replace(publication, journal_path=journal.with_name("foreign-journal"))
                elif changed_binding == "journal-inode":
                    publication = dataclasses.replace(
                        publication, journal_identity=dataclasses.replace(
                            publication.journal_identity, inode=publication.journal_identity.inode + 1,
                        ),
                    )
                elif changed_binding == "parents":
                    publication = dataclasses.replace(
                        publication, parent_guards=dataclasses.replace(publication.parent_guards, guards=()),
                    )
                else:
                    capture = publication.preimages[target]
                    changed_capture = dataclasses.replace(
                        capture, identity=dataclasses.replace(capture.identity, inode=capture.identity.inode + 1),
                    )
                    publication = dataclasses.replace(publication, preimages={target: changed_capture})
                callback(publication)
            kwargs["after_recovery"] = changed
        return publish(writes, **kwargs)

    def checkpoint():
        activation._write(record_path, record)
        if record.get("rollback_publication", {}).get("state") == "published":
            raise RuntimeError("inverse checkpoint failed")

    with monkeypatch.context() as corrupt:
        corrupt.setattr(release_manager, "_publish_files_transactionally", corrupted_recovery)
        with pytest.raises(activation.WebActivationError):
            activation._restore(record, tx, checkpoint=checkpoint)
    assert intercepted
    assert journal.exists()
    assert activation._read(record_path)["rollback_publication"]["state"] == "published"
    assert target.read_bytes() == b"candidate B"
    _restore_in_new_process(record_path)
    assert target.read_bytes() == b"original A"
