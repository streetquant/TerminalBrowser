"""Bounded executable payloads in descriptor-owned publication journals."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from termdeck import release_manager as release
from termdeck import web_activation as activation


OLD_SIZE = 5_219_640
NEW_SIZE = 5_847_728
OLD_SHA256 = "2f9df7f5b23a792d8909f313630a96ef9f849b6e737717a688ef9eb02f47d3da"
NEW_SHA256 = "41eb75fc60ce11db1665d4493282c1f2bb69dd0b63c3fd0f68f860a0edc8fd46"


@pytest.fixture(scope="module")
def binary_pair():
    # These are deliberately incompressible. Capacity must not depend on an
    # executable happening to compress well, or on a repetitive test payload.
    return random.Random(51).randbytes(OLD_SIZE), random.Random(58).randbytes(NEW_SIZE)


def _target(tmp_path, old=b"original A"):
    target = tmp_path / "backend"
    target.write_bytes(old)
    target.chmod(0o755)
    return target, tmp_path / ".termdeck-publication-capacity.json"


def _keep_journal(monkeypatch):
    def retain(self):
        self.close()
        return False
    monkeypatch.setattr(release._PublicationJournal, "finalize_terminal", retain)


def _wire(content):
    payloads, context, digest, offset = release._publication_read_payload_prefix(content)
    prefix = content[:offset]
    frames = []
    while offset < len(content):
        assert content[offset:offset + 5] == b"TDJ1:"
        length = int(content[offset + 5:offset + 13], 16)
        frames.append(json.loads(content[offset + 79:offset + 79 + length]))
        offset += 79 + length
    return prefix, payloads, context, digest, frames


def _run_child(program, *arguments, expected=0):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", program, *map(str, arguments)],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == expected, result.stderr
    return result


def test_real_sized_incompressible_publication_keeps_one_payload_copy(tmp_path, monkeypatch, binary_pair, record_property):
    old, new = binary_pair
    target, journal = _target(tmp_path, old)
    _keep_journal(monkeypatch)
    receipts = []
    release._publish_files_transactionally(
        ((target, new, 0o755, "disposable executable"),), journal_path=journal,
        before_publish=receipts.append, after_publish=receipts.append,
    )
    assert target.read_bytes() == new
    assert [receipt.state for receipt in receipts] == ["intent", "published"]
    assert receipts[0].journal_identity.inode == receipts[1].journal_identity.inode == journal.stat().st_ino
    raw = journal.read_bytes()
    prefix, payloads, context, _digest, frames = _wire(raw)
    assert set(payloads.values()) == {old, new}
    assert len(prefix) < OLD_SIZE + NEW_SIZE + 16 * 1024
    assert len(raw) < OLD_SIZE + NEW_SIZE + 256 * 1024
    assert max(len(release.canonical_json_bytes(frame)) for frame in frames) < 16 * 1024
    assert frames[-1]["state"] == "committed"
    assert all("content_b64" not in json.dumps(frame) for frame in frames)
    assert all("snapshot" not in json.dumps(frame) and "parents" not in frame for frame in frames)
    assert context["records"][0]["path"] == str(target)
    assert journal.stat().st_mode & 0o777 == 0o600
    for key, value in {
        "journal_bytes": len(raw), "payload_prefix_bytes": len(prefix), "frame_count": len(frames),
        "max_state_bytes": max(len(release.canonical_json_bytes(frame)) for frame in frames),
        "original_bytes": OLD_SIZE, "candidate_bytes": NEW_SIZE,
    }.items():
        record_property(key, value)


def test_real_sized_failed_publication_restores_original_inode(tmp_path, monkeypatch, binary_pair):
    old, new = binary_pair
    target, journal = _target(tmp_path, old)
    original = target.stat()
    _keep_journal(monkeypatch)
    recovered = []

    def reject():
        raise RuntimeError("disposable verification rejection")

    with pytest.raises(release.ReleaseError, match="failed and was rolled back"):
        release._publish_files_transactionally(
            ((target, new, 0o755, "disposable executable"),), journal_path=journal,
            verify=reject, after_recovery=recovered.append,
        )
    current = target.stat()
    assert target.read_bytes() == old
    assert (current.st_ino, current.st_mode, current.st_mtime_ns) == (
        original.st_ino, original.st_mode, original.st_mtime_ns,
    )
    assert len(recovered) == 1 and recovered[0].state == "recovered"
    prefix, _payloads, _context, _digest, frames = _wire(journal.read_bytes())
    assert frames[-1]["state"] == "rolled-back"
    assert journal.stat().st_size - len(prefix) < 256 * 1024


PUBLISH_AND_EXIT = """
import os, sys
from pathlib import Path
from termdeck import release_manager as r
target, candidate, journal = map(Path, sys.argv[1:])
r._publish_files_transactionally(
    ((target, candidate.read_bytes(), 0o755, 'disposable executable'),),
    journal_path=journal, after_publish=lambda receipt: os._exit(73),
)
"""


RECOVER = """
import json, sys
from termdeck import release_manager as r
print(json.dumps(r.recover_publication_transaction(sys.argv[1])))
"""


def test_real_sized_journal_recovers_in_fresh_process(tmp_path, binary_pair):
    old, new = binary_pair
    target, journal = _target(tmp_path, old)
    old_inode = target.stat().st_ino
    candidate = tmp_path / "candidate-input"
    candidate.write_bytes(new)
    _run_child(PUBLISH_AND_EXIT, target, candidate, journal, expected=73)
    assert target.read_bytes() == new
    original_journal_inode = journal.stat().st_ino
    assert release._read_publication_journal(journal)[2]["state"] == "active"
    result = json.loads(_run_child(RECOVER, journal).stdout)
    assert result["state"] == "rolled-back"
    assert target.read_bytes() == old and target.stat().st_ino == old_inode
    assert not journal.exists()
    assert original_journal_inode > 0


def test_interrupted_payload_initialization_does_not_publish(tmp_path, monkeypatch, binary_pair):
    old, new = binary_pair
    target, journal = _target(tmp_path, old)
    original = target.stat()
    writer = release._write_all
    callbacks = []

    def partial(descriptor, content, *, label):
        if label == "publication immutable payloads":
            os.write(descriptor, content[:100])
            os.fsync(descriptor)
            raise OSError("disposable prefix interruption")
        return writer(descriptor, content, label=label)

    monkeypatch.setattr(release, "_write_all", partial)
    with pytest.raises(release.ReleaseError, match="initialization was not durably resolved"):
        release._publish_files_transactionally(
            ((target, new, 0o755, "disposable executable"),),
            journal_path=journal, before_publish=callbacks.append,
        )
    assert callbacks == []
    assert target.read_bytes() == old
    assert target.stat() == original
    retained = journal.read_bytes()
    assert len(retained) == 100
    with pytest.raises(release.ReleaseError, match="truncated|changed"):
        release.recover_publication_transaction(journal)
    assert journal.read_bytes() == retained and target.read_bytes() == old


def test_torn_state_append_survives_interrupted_restart(tmp_path, binary_pair):
    old, new = binary_pair
    target, journal = _target(tmp_path, old)
    candidate = tmp_path / "candidate-input"
    candidate.write_bytes(new)
    program = """
import json, os, sys
from pathlib import Path
from termdeck import release_manager as r
writer = r._write_all
def torn(fd, content, *, label):
    if label == 'publication journal' and json.loads(content[79:])['state'] == 'committed':
        os.write(fd, content[:100]); os.fsync(fd); os._exit(74)
    return writer(fd, content, label=label)
r._write_all = torn
target, candidate, journal = map(Path, sys.argv[1:])
r._publish_files_transactionally(((target, candidate.read_bytes(), 0o755, 'disposable executable'),), journal_path=journal)
"""
    _run_child(program, target, candidate, journal, expected=74)
    inode = journal.stat().st_ino
    assert release._read_publication_journal(journal)[2]["state"] == "active"
    interrupt = """
import os, sys
from termdeck import release_manager as r
r.recover_publication_transaction(sys.argv[1], after_recovery=lambda receipt: os._exit(75))
"""
    _run_child(interrupt, journal, expected=75)
    assert journal.stat().st_ino == inode
    assert target.read_bytes() == old
    assert json.loads(_run_child(RECOVER, journal).stdout)["state"] == "rolled-back"
    assert target.read_bytes() == old and not journal.exists()


@pytest.mark.parametrize("case", ["new-file", "old-file", "logical-total", "record-count", "frame", "lifecycle"])
def test_unsupported_capacity_is_rejected_before_any_create(tmp_path, monkeypatch, case):
    missing = tmp_path / "must-not-be-created"
    target = missing / "target"
    writes = [(target, b"new", 0o755, "capacity rejection")]
    if case == "new-file":
        writes[0] = (target, b"x" * (release._MAX_PUBLICATION_PAYLOAD_FILE_BYTES + 1), 0o755, "too large")
    elif case == "old-file":
        existing = tmp_path / "existing"
        with existing.open("wb") as output:
            output.truncate(release._MAX_PUBLICATION_PAYLOAD_FILE_BYTES + 1)
        writes.append((existing, b"new", 0o755, "oversize original"))
    elif case == "logical-total":
        block = b"x" * release._MAX_PUBLICATION_PAYLOAD_FILE_BYTES
        writes = [(missing / str(index), block, 0o755, "repeated logical bytes") for index in range(3)]
    elif case == "record-count":
        writes *= release._MAX_PUBLICATION_RECORDS + 1
        writes = [(missing / str(index), b"new", 0o755, "too many") for index in range(len(writes))]
    elif case == "frame":
        writes[0] = (target, b"new", 0o755, "x" * release._MAX_PUBLICATION_JOURNAL_FRAME_BYTES)
    else:
        writes[0] = (target, b"new", 0o755, "x" * (2 * 1024 * 1024))
    opener = release._openat

    def read_only(parent, name, flags, **kwargs):
        assert not flags & os.O_CREAT, "capacity rejection created a journal or stage"
        return opener(parent, name, flags, **kwargs)

    monkeypatch.setattr(release, "_openat", read_only)
    with pytest.raises(release.ReleaseError, match="capacity|too large"):
        release._publish_files_transactionally(tuple(writes))
    assert not missing.exists()
    assert not list(tmp_path.glob(".termdeck-publication-*"))


def test_journal_total_size_is_checked_before_reading_sparse_file(tmp_path, monkeypatch):
    journal = tmp_path / ".termdeck-publication-oversize.json"
    with journal.open("wb") as output:
        output.truncate(release._MAX_PUBLICATION_JOURNAL_BYTES + 1)
    journal.chmod(0o600)
    monkeypatch.setattr(release.os, "read", lambda *_args: pytest.fail("oversize journal was read"))
    with pytest.raises(release.ReleaseError, match="capture capacity"):
        release.recover_publication_transaction(journal)
    assert journal.exists()


def test_append_capacity_exhaustion_preserves_receipt_before_staging(tmp_path, monkeypatch):
    target, journal = _target(tmp_path)
    original = target.stat()
    with monkeypatch.context() as limited:
        def exhaust(receipt):
            limited.setattr(release, "_MAX_PUBLICATION_JOURNAL_BYTES", journal.stat().st_size + 1)
        with pytest.raises(release.ReleaseError, match="durability is uncertain"):
            release._publish_files_transactionally(
                ((target, b"candidate B", 0o755, "bounded append"),),
                journal_path=journal, before_publish=exhaust,
            )
        assert journal.stat().st_size < release._MAX_PUBLICATION_JOURNAL_BYTES
    assert target.read_bytes() == b"original A" and target.stat() == original
    assert release.recover_publication_transaction(journal)["state"] == "rolled-back"
    assert not journal.exists()


@pytest.fixture
def compact_journal(tmp_path, monkeypatch):
    target, journal = _target(tmp_path)
    _keep_journal(monkeypatch)
    release._publish_files_transactionally(((target, b"candidate B", 0o755, "fixture"),), journal_path=journal)
    return target, journal, _wire(journal.read_bytes())


@pytest.mark.parametrize("cut", [1, 4, 5, 12, 13, 14, 40, 78, 79, 100])
def test_each_torn_header_boundary_preserves_complete_prefix(compact_journal, cut):
    _target_path, _journal, (prefix, _payloads, _context, _digest, frames) = compact_journal
    first = release._publication_journal_frame(frames[0])
    following = release._publication_journal_frame(frames[1])
    payload, complete = release._publication_journal_decode(prefix + first + following[:cut])
    assert payload["generation"] == frames[0]["generation"]
    assert complete == len(prefix) + len(first)


@pytest.mark.parametrize("fault", ["fsync", "foreign-inode", "hardlink", "symlink", "malformed-complete-frame"])
def test_torn_tail_repair_preserves_target_and_foreign_authority(tmp_path, monkeypatch, fault):
    target, journal = _target(tmp_path)
    candidate = tmp_path / "candidate-input"
    candidate.write_bytes(b"candidate B")
    _run_child(PUBLISH_AND_EXIT, target, candidate, journal, expected=73)
    raw = journal.read_bytes()
    prefix, _payloads, _context, _digest, frames = _wire(raw)
    next_state = dict(frames[-1], generation=frames[-1]["generation"] + 1)
    next_frame = release._publication_journal_frame(next_state)
    tail = next_frame[:100]
    if fault == "malformed-complete-frame":
        tail = next_frame[:-1] + bytes([next_frame[-1] ^ 1])
    journal.write_bytes(raw + tail)
    original = target.stat()
    if fault == "fsync":
        sync = release._fsync_descriptor
        def reject_tail_sync(descriptor, *, label):
            if label == "publication journal tail recovery":
                raise release.ReleaseError("disposable tail fsync rejection")
            return sync(descriptor, label=label)
        monkeypatch.setattr(release, "_fsync_descriptor", reject_tail_sync)
    elif fault == "foreign-inode":
        opener = release._open_recovery_journal
        def replace_after_capture(*args, **kwargs):
            foreign = journal.with_name("foreign-journal")
            foreign.write_bytes(raw + tail)
            foreign.chmod(0o600)
            foreign.replace(journal)
            return opener(*args, **kwargs)
        monkeypatch.setattr(release, "_open_recovery_journal", replace_after_capture)
    elif fault == "hardlink":
        os.link(journal, journal.with_name("linked-journal"))
    elif fault == "symlink":
        saved = journal.with_name("saved-journal")
        journal.rename(saved)
        journal.symlink_to(saved)
    with pytest.raises(release.ReleaseError):
        release.recover_publication_transaction(journal)
    assert target.read_bytes() == b"candidate B" and target.stat() == original
    assert journal.read_bytes() == (raw if fault == "fsync" else raw + tail)
    assert journal.read_bytes().startswith(prefix)


def _prefix_with_manifest(manifest, blobs=b""):
    raw = release.canonical_json_bytes(manifest)
    return b"TDP1:" + f"{len(raw):08x}:".encode() + hashlib.sha256(raw).hexdigest().encode() + b"\n" + raw + blobs


@pytest.mark.parametrize("case", [
    "prefix-header", "manifest-truncated", "payload-truncated", "manifest-digest", "payload-digest",
    "duplicate-payload", "payload-oversize", "unknown-reference", "reference-size", "reference-bool",
    "manifest-binding", "record-binding", "duplicate-generation", "unreferenced-payload", "logical-amplification",
])
def test_malformed_compact_journal_never_authorizes_mutation(compact_journal, case):
    target, journal, (prefix, payloads, context, digest, frames) = compact_journal
    context = copy.deepcopy(context)
    first = copy.deepcopy(frames[0])
    tail = release._publication_journal_frame(first)
    raw = prefix + tail
    if case == "prefix-header":
        raw = b"TDP1:zzzzzzzz:" + raw[14:]
    elif case == "manifest-truncated":
        raw = prefix[:100]
    elif case == "payload-truncated":
        raw = prefix[:-1]
    elif case == "manifest-digest":
        raw = prefix[:14] + b"0" * 64 + prefix[78:] + tail
    elif case == "payload-digest":
        raw = prefix[:-1] + bytes([prefix[-1] ^ 1]) + tail
    elif case in {"duplicate-payload", "payload-oversize"}:
        item = {"sha256": sorted(payloads)[0], "size": len(payloads[sorted(payloads)[0]])}
        entries = [item, item] if case == "duplicate-payload" else [dict(item, size=release._MAX_PUBLICATION_PAYLOAD_FILE_BYTES + 1)]
        raw = _prefix_with_manifest({"schema": release._PUBLICATION_PAYLOAD_SCHEMA, "blobs": entries, "context": context}, payloads[item["sha256"]])
    elif case == "unknown-reference":
        context["records"][0]["content_ref"]["sha256"] = "0" * 64
        prefix = release._publication_payload_prefix(payloads, context)
        first["payloads_sha256"] = release.sha256_bytes(release._publication_payload_manifest(payloads, context))
        raw = prefix + release._publication_journal_frame(first)
    elif case in {"reference-size", "reference-bool"}:
        context["records"][0]["content_ref"]["size"] = True if case == "reference-bool" else 999
        prefix = release._publication_payload_prefix(payloads, context)
        first["payloads_sha256"] = release.sha256_bytes(release._publication_payload_manifest(payloads, context))
        raw = prefix + release._publication_journal_frame(first)
    elif case == "manifest-binding":
        first["payloads_sha256"] = "0" * 64
        raw = prefix + release._publication_journal_frame(first)
    elif case == "record-binding":
        changed = copy.deepcopy(first)
        changed["generation"] += 1
        changed["records"][0]["label"] = "changed immutable record"
        raw += release._publication_journal_frame(changed)
    elif case == "duplicate-generation":
        raw += tail
    elif case == "unreferenced-payload":
        more = {**payloads, release.sha256_bytes(b"unused"): b"unused"}
        prefix = release._publication_payload_prefix(more, context)
        first["payloads_sha256"] = release.sha256_bytes(release._publication_payload_manifest(more, context))
        raw = prefix + release._publication_journal_frame(first)
    else:
        block = b"x" * (release._MAX_PUBLICATION_PAYLOAD_FILE_BYTES // 2 + 1)
        large = {release.sha256_bytes(block): block}
        ref = {"sha256": release.sha256_bytes(block), "size": len(block)}
        row = context["records"][0]
        row["content_ref"] = ref
        row["snapshot"]["content_ref"] = ref
        context["records"] = [row, copy.deepcopy(row)]
        first["records"] *= 2
        first["payloads_sha256"] = release.sha256_bytes(release._publication_payload_manifest(large, context))
        raw = release._publication_payload_prefix(large, context) + release._publication_journal_frame(first)
    journal.write_bytes(raw)
    original = target.stat()
    with pytest.raises(release.ReleaseError):
        release.recover_publication_transaction(journal)
    assert target.read_bytes() == b"candidate B" and target.stat() == original
    assert journal.read_bytes() == raw


def test_legacy_inline_journal_remains_restart_recoverable(tmp_path):
    target, journal = _target(tmp_path)
    candidate = tmp_path / "candidate-input"
    candidate.write_bytes(b"candidate B")
    _run_child(PUBLISH_AND_EXIT, target, candidate, journal, expected=73)
    # Encode the existing exact receipt using the historical inline wire
    # format, preserving all snapshots and inode guards in this fixture.
    payload = release._read_publication_journal(journal)[2]
    journal.write_bytes(release._publication_journal_frame(payload))
    assert journal.read_bytes().startswith(b"TDJ1:")
    assert json.loads(_run_child(RECOVER, journal).stdout)["state"] == "rolled-back"
    assert target.read_bytes() == b"original A" and not journal.exists()


@pytest.mark.parametrize("interrupted_inverse", [False, True], ids=["complete", "restart-inverse"])
def test_actual_executables_activation_callbacks_and_inverse(tmp_path, interrupted_inverse, record_property):
    inputs = [os.environ.get("TERMDECK_JOURNAL_REAL_A"), os.environ.get("TERMDECK_JOURNAL_REAL_B")]
    if not all(inputs):
        pytest.skip("explicit read-only executable inputs were not supplied")
    originals = []
    for raw_path, size, digest in zip(inputs, (OLD_SIZE, NEW_SIZE), (OLD_SHA256, NEW_SHA256)):
        path = Path(raw_path)
        assert path.is_file() and not path.is_symlink() and path.stat().st_size == size
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest
        originals.append(content)
    old, new = originals
    record_property("original_sha256", OLD_SHA256)
    record_property("candidate_sha256", NEW_SHA256)
    record_property("original_bytes", len(old))
    record_property("candidate_bytes", len(new))
    target, _journal = _target(tmp_path, old)
    tx = tmp_path / "transaction"
    tx.mkdir(mode=0o700)
    record_path = tx / "record.json"
    record = activation._preimage(target, tx, "backend")
    checkpoints = []

    def persist():
        checkpoints.append(copy.deepcopy(record))
        activation._write(record_path, record)

    activation._publish_preimaged(
        record, tx, new, 0o755, label="disposable actual executable",
        intent_checkpoint=persist, checkpoint=persist,
    )
    assert target.read_bytes() == new
    assert record["postimage"]["sha256"] == NEW_SHA256
    bound = [row["publication_intent"]["journal_identity"] for row in checkpoints if row["publication_intent"].get("journal_identity")]
    assert bound and len({item["inode"] for item in bound}) == 1
    if interrupted_inverse:
        interruption = """
import os, sys
from pathlib import Path
from termdeck import web_activation as a
path = Path(sys.argv[1]); record = a._read(path)
def checkpoint():
    a._write(path, record)
    if record.get('rollback_publication', {}).get('state') == 'published':
        os._exit(76)
a._restore(record, path.parent, checkpoint=checkpoint)
"""
        _run_child(interruption, record_path, expected=76)
        persisted = activation._read(record_path)
        inverse = persisted["rollback_publication"]
        assert inverse["state"] == "published"
        program = """
import json, sys
from pathlib import Path
from termdeck import web_activation as a
path = Path(sys.argv[1]); record = a._read(path)
print(json.dumps(a._restore(record, path.parent, checkpoint=lambda: a._write(path, record))))
"""
        _run_child(program, record_path)
    else:
        activation._restore(record, tx, checkpoint=persist)
    assert target.read_bytes() == old
    assert target.stat().st_mode & 0o777 == 0o755
    for raw_path, digest in zip(inputs, (OLD_SHA256, NEW_SHA256)):
        assert hashlib.sha256(Path(raw_path).read_bytes()).hexdigest() == digest
