import hashlib
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
from unittest import mock

import pytest

from termdeck.output_journal import MAGIC, OutputJournal, read_page


def test_record_is_durable_before_consumer_and_survives_sigkill(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", """
from pathlib import Path
from termdeck.output_journal import OutputJournal
import sys, time
j = OutputJournal(Path(sys.argv[1]), 'saved-tab')
j.append(b'full screen', geometry=(91, 31))
j.append(b'unsaved command output\\r\\n\\xff')
print('display permitted', flush=True)
time.sleep(60)
""", str(tmp_path / "durability")], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "display permitted"
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        page = read_page(tmp_path / "durability", "saved-tab")
        assert page["complete"] and not page["truncated_tail"]
        assert [(e["kind"], bytes.fromhex(e["data_hex"])) for e in page["events"]] == [
            (0, b"full screen"), (1, b"unsaved command output\r\n\xff")]
        assert (page["events"][0]["cols"], page["events"][0]["rows"]) == (91, 31)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_partial_tail_is_ignored_but_checksum_corruption_is_rejected(tmp_path):
    journal = OutputJournal(tmp_path / "durability", "saved-tab")
    journal.append(b"preserved", geometry=(80, 24))
    journal.close()
    with journal.path.open("ab") as stream:
        stream.write(struct.pack(">IB", 20, 1) + b"partial")
    page = read_page(tmp_path / "durability", "saved-tab")
    assert page["complete"] and page["truncated_tail"]
    assert bytes.fromhex(page["events"][0]["data_hex"]) == b"preserved"
    with journal.path.open("r+b") as stream:
        stream.seek(len(MAGIC) + 10)
        stream.write(b"X")
    with pytest.raises(ValueError, match="checksum"):
        read_page(tmp_path / "durability", "saved-tab")


def test_failed_fsync_permanently_stops_forward_permission(tmp_path):
    journal = OutputJournal(tmp_path / "durability", "saved-tab")
    journal.append(b"first")
    with mock.patch("termdeck.output_journal.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            journal.append(b"must not display")
    with pytest.raises(OSError, match="previously failed"):
        journal.append(b"not a safe retry")
    journal.close()


def test_generations_retained_with_private_files_and_paginated_bytes(tmp_path):
    root = tmp_path / "durability"
    first = OutputJournal(root, "saved-tab")
    first.append(b"old app output")
    second = OutputJournal(root, "saved-tab")
    second.append(b"new shell")
    second.append(b"next page")
    page = read_page(root, "saved-tab", limit=1)
    assert bytes.fromhex(page["events"][0]["data_hex"]) == b"new shell"
    page2 = read_page(root, "saved-tab", page["generation"], page["next_offset"])
    assert bytes.fromhex(page2["events"][0]["data_hex"]) == b"next page"
    assert bytes.fromhex(read_page(root, "saved-tab", first.generation)["events"][0]["data_hex"]) == b"old app output"
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert first.directory.stat().st_mode & 0o777 == 0o700
    first.close(); second.close()


def test_symlinks_hardlinks_and_path_escape_rejected(tmp_path):
    root = tmp_path / "durability"
    journal = OutputJournal(root, "saved-tab")
    journal.append(b"private")
    journal.close()
    with pytest.raises(ValueError):
        read_page(root, "../escape")
    with pytest.raises(ValueError):
        read_page(root, "saved-tab", "../../secret")
    os.link(journal.path, tmp_path / "hardlink")
    with pytest.raises(OSError, match="unsafe"):
        read_page(root, "saved-tab")
    (tmp_path / "hardlink").unlink()
    journal.path.unlink()
    journal.path.symlink_to(tmp_path / "secret")
    with pytest.raises(OSError):
        read_page(root, "saved-tab")


def test_frame_format_has_cross_language_sha256_definition(tmp_path):
    journal = OutputJournal(tmp_path / "durability", "saved-tab")
    journal.append(b"\x00\xff\r\n")
    assert journal.path.read_bytes() == MAGIC + struct.pack(">IB", 4, 1) + b"\x00\xff\r\n" + hashlib.sha256(b"\x01\x00\xff\r\n").digest()
    journal.close()


def test_archive_reader_cannot_display_a_complete_unflushed_append(tmp_path):
    import threading
    journal = OutputJournal(tmp_path / "durability", "saved-tab")
    journal.append(b"already committed")
    entered, release = threading.Event(), threading.Event()
    actual_fsync = os.fsync
    def delayed(fd):
        if fd == journal._stream.fileno():
            entered.set()
            assert release.wait(5)
        actual_fsync(fd)
    with mock.patch("termdeck.output_journal.os.fsync", side_effect=delayed):
        thread = threading.Thread(target=journal.append, args=(b"not committed yet",))
        thread.start()
        try:
            assert entered.wait(5)
            page = read_page(tmp_path / "durability", "saved-tab")
            assert [bytes.fromhex(e["data_hex"]) for e in page["events"]] == [b"already committed"]
            assert page["truncated_tail"]
        finally:
            release.set()
            thread.join(5)
    assert not thread.is_alive()
    assert len(read_page(tmp_path / "durability", "saved-tab")["events"]) == 2
    journal.close()


def test_independent_tabs_flush_concurrently_and_screens_do_not_block_them(tmp_path):
    import threading
    from termdeck.session_durability import _lock
    root = tmp_path / "durability"
    first, second = OutputJournal(root, "first"), OutputJournal(root, "second")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    actual_fsync = os.fsync
    errors = []
    def delayed(fd):
        if fd == first._stream.fileno():
            entered.set()
            assert release.wait(5)
        actual_fsync(fd)
    def append(journal, payload, finished=None):
        try:
            journal.append(payload)
            if finished:
                finished.set()
        except BaseException as exc:
            errors.append(exc)
    with _lock(root), mock.patch("termdeck.output_journal.os.fsync", side_effect=delayed):
        slow = threading.Thread(target=append, args=(first, b"slow"))
        fast = threading.Thread(target=append, args=(second, b"independent", done))
        slow.start()
        try:
            assert entered.wait(5)
            fast.start()
            assert done.wait(2), "unrelated tab or screen snapshot blocked the writer"
        finally:
            release.set()
            slow.join(5)
            if fast.ident is not None:
                fast.join(5)
    assert not errors
    first.close(); second.close()


def test_copy_lock_keeps_data_and_commit_boundary_consistent(tmp_path):
    import fcntl
    import shutil
    import threading
    root = tmp_path / "durability"
    journal = OutputJournal(root, "saved-tab")
    journal.append(b"before backup")
    started, done = threading.Event(), threading.Event()
    def append():
        started.set()
        journal.append(b"after backup")
        done.set()
    with (root / ".journal-copy.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        worker = threading.Thread(target=append)
        worker.start()
        assert started.wait(2)
        try:
            assert not done.wait(.05)
            shutil.copytree(root, tmp_path / "mirror")
            page = read_page(tmp_path / "mirror", "saved-tab")
            assert not page["truncated_tail"]
            assert [bytes.fromhex(e["data_hex"]) for e in page["events"]] == [b"before backup"]
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            worker.join(5)
    assert done.is_set()
    assert len(read_page(root, "saved-tab")["events"]) == 2
    journal.close()


def test_view_journals_never_replace_the_canonical_background_source(tmp_path):
    root = tmp_path / "durability"
    view = OutputJournal(root, "tab", primary=False)
    view.append(b"initial visible output")
    assert read_page(root, "tab")["generation"] == view.generation
    canonical = OutputJournal(root, "tab")
    canonical.append(b"background start", geometry=(91, 27))
    view.append(b"displayed in first browser")
    later_view = OutputJournal(root, "tab", primary=False)
    later_view.append(b"second browser reset", geometry=(80, 24))
    canonical.append(b"while every browser is closed")
    later_view.append(b"last foreground write")
    assert read_page(root, "tab")["generation"] == later_view.generation
    assert (canonical.directory / "latest").read_text() == canonical.generation
    page = read_page(root, "tab", canonical.generation)
    assert [bytes.fromhex(e["data_hex"]) for e in page["events"]] == [
        b"background start", b"while every browser is closed"]
    assert len(read_page(root, "tab", view.generation)["events"]) == 2
    assert read_page(root, "tab", later_view.generation)["events"][0]["cols"] == 80
    view.close(); later_view.close(); canonical.close()


def test_mirror_copies_only_committed_prefix_and_retains_prior_generations(tmp_path):
    from termdeck.output_journal import mirror_journals
    root, target = tmp_path / "source", tmp_path / "ssd"
    journal = OutputJournal(root, "tab")
    journal.append(b"committed before crash", geometry=(91, 27))
    with journal.path.open("ab") as raw:
        raw.write(b"incomplete frame")
    assert mirror_journals(root, target)["copied"] == 1
    page = read_page(target, "tab")
    assert not page["truncated_tail"] and len(page["events"]) == 1
    assert bytes.fromhex(page["events"][0]["data_hex"]) == b"committed before crash"
    journal.close()
    new = OutputJournal(root, "tab")
    new.append(b"new application")
    result = mirror_journals(root, target)
    assert result["copied"] == 1 and result["skipped"] == 1
    assert read_page(target, "tab")["generation"] == new.generation
    assert read_page(target, "tab", journal.generation)["events"] == page["events"]
    new.close()


def test_mirror_does_not_block_new_appends_or_copy_future_watermark(tmp_path):
    import threading
    from termdeck.output_journal import mirror_journals
    root, target = tmp_path / "source", tmp_path / "ssd"
    journal = OutputJournal(root, "tab")
    journal.append(b"before mirror")
    entered, release = threading.Event(), threading.Event()
    actual_sync = os.fsync
    errors = []
    def delayed(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if str(target) in path and ".tdj-" in path:
            entered.set()
            assert release.wait(5)
        actual_sync(fd)
    def copy():
        try:
            mirror_journals(root, target)
        except BaseException as exc:
            errors.append(exc)
    with mock.patch("termdeck.output_journal.os.fsync", side_effect=delayed):
        worker = threading.Thread(target=copy)
        worker.start()
        try:
            assert entered.wait(5)
            journal.append(b"while SSD copy is paused")
        finally:
            release.set()
            worker.join(5)
    assert not errors and not worker.is_alive()
    assert len(read_page(root, "tab")["events"]) == 2
    assert len(read_page(target, "tab")["events"]) == 1
    mirror_journals(root, target)
    assert len(read_page(target, "tab")["events"]) == 2
    journal.close()


def test_stalled_writer_backlog_closes_only_disposable_sink(tmp_path):
    import shlex
    import time
    runtime = tmp_path / 'tmux'
    runtime.mkdir(mode=0o700)
    env = dict(os.environ, TMUX_TMPDIR=str(runtime))
    for key in ('TMUX', 'TMUX_PANE', 'TERMDECK_TMUX_RUNTIME', 'TERMDECK_TMUX_IDENTITY'):
        env.pop(key, None)
    def tmux(*args):
        return subprocess.check_output(['/usr/bin/tmux', *args], env=env, text=True, timeout=5).strip()
    def rss(pid):
        return next(int(line.split()[1]) for line in Path(f'/proc/{pid}/status').read_text().splitlines()
                    if line.startswith('VmRSS:'))
    script = tmp_path / 'stalled-sink.py'
    script.write_text('''import os,sys,time
sys.path.insert(0,sys.argv[3])
from pathlib import Path
from termdeck.output_journal import OutputJournal,_pipe_input
j=OutputJournal(Path(sys.argv[1]),'tab')
j.append(b'committed before stalled writer')
pending=_pipe_input(0)
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(30) # Disk writer intentionally makes no progress; reader must bound it.
''')
    root, identity = tmp_path / 'durability', tmp_path / 'sink-pid'
    try:
        tmux('new-session', '-d', '-s', 'disposable', '-x', '91', '-y', '27',
             '/bin/bash --noprofile --norc')
        before = tmux('display-message', '-p', '-t', 'disposable', '#{pane_pid} #{pane_width} #{pane_height}')
        server = int(tmux('display-message', '-p', '#{pid}'))
        initial_rss = peak_rss = rss(server)
        command = 'exec ' + shlex.join([sys.executable, str(script), str(root), str(identity), str(Path(__file__).resolve().parents[1])])
        tmux('pipe-pane', '-O', '-t', 'disposable', command)
        deadline = time.monotonic() + 10
        while not identity.exists():
            assert time.monotonic() < deadline
            time.sleep(.005)
        tmux('send-keys', '-t', 'disposable', '-l', "head -c 16777216 /dev/zero | tr '\\000' Q")
        tmux('send-keys', '-t', 'disposable', 'Enter')
        while tmux('display-message', '-p', '-t', 'disposable', '#{pane_pipe}') != '0':
            peak_rss = max(peak_rss, rss(server))
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert peak_rss - initial_rss < 64 * 1024
        assert not Path('/proc/' + identity.read_text()).exists()
        marker = 'APP_STILL_RUNNING_AFTER_BACKLOG'
        escaped = ''.join(f'\\x{byte:02x}' for byte in marker.encode())
        tmux('send-keys', '-t', 'disposable', '-l', "printf '\\n" + escaped + "\\n'")
        tmux('send-keys', '-t', 'disposable', 'Enter')
        while marker not in tmux('capture-pane', '-p', '-t', 'disposable'):
            peak_rss = max(peak_rss, rss(server))
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert peak_rss - initial_rss < 64 * 1024
        assert tmux('display-message', '-p', '-t', 'disposable', '#{pane_pid} #{pane_width} #{pane_height}') == before
        assert [bytes.fromhex(e['data_hex']) for e in read_page(root, 'tab')['events']] == [b'committed before stalled writer']
    finally:
        subprocess.run(['/usr/bin/tmux', 'kill-server'], env=env, capture_output=True, timeout=5)


def test_compact_journals_compresses_old_closed_generations_and_keeps_readable(tmp_path):
    from termdeck.output_journal import compact_journals
    root = tmp_path / "durability"
    gens = []
    for index in range(4):
        j = OutputJournal(root, "tab")
        j.append(f"frame-{index}-".encode() * 64, geometry=(91, 27))
        j.append(b"second-frame", geometry=None)
        j.close()
        gens.append(j.generation)
        old_ts = 1_700_000_000 + index
        os.utime(j.directory / j.generation, (old_ts, old_ts))
    view = OutputJournal(root, "tab", primary=False)
    view.append(b"view-active")
    view.close()
    os.utime(view.directory / view.generation, (1_700_000_010, 1_700_000_010))

    summary = compact_journals(root, keep_recent=1, min_age_seconds=60)
    assert summary["failed"] == 0
    assert summary["compressed"] >= 2
    assert (root / "journals/tab" / gens[-1]).exists()
    assert (root / "journals/tab" / view.generation).exists()
    for archived in gens[:2]:
        assert not (root / "journals/tab" / archived).exists()
        assert (root / "journals/tab" / f"{archived}.zst").exists()
        page = read_page(root, "tab", archived, limit=1)
        assert not page["complete"] and not page["truncated_tail"]
        page_tail = read_page(root, "tab", archived, offset=page["next_offset"])
        assert page_tail["complete"]
        data = b"".join(bytes.fromhex(e["data_hex"]) for e in (page["events"] + page_tail["events"]))
        assert data == f"frame-{gens.index(archived)}-".encode() * 64 + b"second-frame"


def test_pipe_owner_accepts_verified_native_recorder_bundle(tmp_path):
    from termdeck.output_journal import pipe_owner
    from termdeck.resources import _parse_stat, _process_argv
    from termdeck.startup_publication import prepare
    src = tmp_path / "src"
    pkg = src / "termdeck"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "startup_recovery.py").write_text("def main(): return 0\n")
    fake_bin = src / "web/target/release/termdeck-record-output"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_bytes(b"#!/bin/sh\necho ok\n")
    fake_bin.chmod(0o755)
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o700)
    bundle = Path(prepare(src, managed)["bundle"])
    assert (bundle / "termdeck-record-output").exists()
    own_pid = os.getpid()
    own_stat = _parse_stat(own_pid)
    parent_pid = own_stat["ppid"]
    parent_stat = _parse_stat(parent_pid)
    root = tmp_path / "durability"
    tab_dir = OutputJournal(root, "tab-native")
    tab_dir.append(b"init")
    token = "0123456789abcdef0123456789abcdef"
    fake_argv = [
        str(bundle / "termdeck-record-output"),
        "--record-output",
        "--root", str(root),
        "--session-id", "tab-native",
        "--pane-id", "%1",
        "--pane-pid", str(own_pid),
        "--pane-start", str(own_stat["starttime"]),
        "--server-pid", str(parent_pid),
        "--server-start", str(parent_stat["starttime"]),
        "--columns", "80",
        "--rows", "24",
        "--token", token,
    ]
    owner_payload = {
        "session_id": "tab-native",
        "pane_id": "%1",
        "pane_pid": own_pid,
        "pane_start": own_stat["starttime"],
        "server_pid": parent_pid,
        "server_start": parent_stat["starttime"],
        "pid": own_pid,
        "starttime": own_stat["starttime"],
        "argv": list(_process_argv(own_pid)),
        "token": token,
        "generation": tab_dir.generation,
    }
    with mock_argv(fake_argv, owner_payload, tab_dir.directory):
        owner = pipe_owner(root, "tab-native", "%1", parent_pid, parent_stat["starttime"])
        assert owner is not None and owner["token"] == token
    tab_dir.close()


def test_compaction_verifies_decompressed_sha256_and_rejects_mismatch_preserving_original(tmp_path):
    import json
    from termdeck.output_journal import (
        compact_journals,
        compact_main,
        list_generations,
    )
    root = tmp_path / "durability"
    gens = []
    raw_hashes = {}
    for idx in range(3):
        j = OutputJournal(root, "tab-sha")
        j.append(f"exact-payload-{idx}-".encode() * 128, geometry=(90, 30))
        j.append(b"\x1b[32mverified-line\x1b[0m\r\n")
        j.close()
        raw_hashes[j.generation] = hashlib.sha256(j.path.read_bytes()).hexdigest()
        os.utime(j.path, (1_700_000_000 + idx, 1_700_000_000 + idx))
        gens.append(j.generation)

    # Inject a simulated decompression SHA-256 mismatch on the first run:
    # original .tdj must NOT be deleted, temp .zst must be cleaned, and compact_main must exit 1.
    with mock.patch(
        "termdeck.output_journal._sha256_zstd_decompressed",
        return_value=("0" * 64, 123),
    ):
        rc = compact_main(["--root", str(root), "--keep-recent", "1", "--min-age-seconds", "60"])
    assert rc == 1
    status_failed = json.loads((root / "compaction-status.json").read_text())
    assert status_failed["status"] == "failed"
    assert status_failed["failed"] == 2
    assert status_failed["errors"][0]["reason"] == "sha256_mismatch"
    for g in gens[:2]:
        assert (root / "journals/tab-sha" / g).exists()
        assert not (root / "journals/tab-sha" / f"{g}.zst").exists()
        assert hashlib.sha256((root / "journals/tab-sha" / g).read_bytes()).hexdigest() == raw_hashes[g]

    # Now run real compaction and prove byte-for-byte decompressed SHA-256 equality + list_generations parity.
    summary = compact_journals(root, keep_recent=1, min_age_seconds=60)
    assert summary["status"] == "ok" and summary["failed"] == 0
    assert summary["compressed"] == 2 and summary["sha256_verified"] == 2
    for g in gens[:2]:
        zst_file = root / "journals/tab-sha" / f"{g}.zst"
        assert zst_file.exists() and not (root / "journals/tab-sha" / g).exists()
        decomp = subprocess.check_output(["/usr/bin/zstd", "-dc", "-q", str(zst_file)])
        assert hashlib.sha256(decomp).hexdigest() == raw_hashes[g]
    assert list_generations(root, "tab-sha") == sorted(gens)


def test_compaction_crash_recovery_non_overlap_and_graceful_interrupt(tmp_path):
    import fcntl
    import json
    import threading
    from termdeck.output_journal import audit_journal_health, compact_journals, list_generations
    root = tmp_path / "durability"
    gens = []
    for idx in range(4):
        j = OutputJournal(root, "tab-crash")
        j.append(f"crash-stage-{idx}-".encode() * 96, geometry=(80, 24))
        j.close()
        os.utime(j.path, (1_700_000_100 + idx, 1_700_000_100 + idx))
        gens.append(j.generation)

    tab_dir = root / "journals/tab-crash"
    # 1. Stage-A crash artifact: orphan temp file left behind.
    orphan = tab_dir / f".{gens[0]}.zst-{'a' * 32}"
    orphan.write_bytes(b"partial-zstd-garbage")
    orphan.chmod(0o600)

    # 2. Stage-B crash artifact on gens[0]: valid .zst already replaced, crash occurred before .tdj unlink.
    valid_zst = tab_dir / f"{gens[0]}.zst"
    with (tab_dir / gens[0]).open("rb") as src_in, valid_zst.open("wb") as dst_out:
        subprocess.run(
            ["/usr/bin/zstd", "-3", "-q", "--no-progress"],
            stdin=src_in,
            stdout=dst_out,
            check=True,
        )
    valid_zst.chmod(0o600)

    # 3. Corrupt pre-existing .zst alongside intact .tdj on gens[1]: must repair from .tdj, never lose data.
    corrupt_zst = tab_dir / f"{gens[1]}.zst"
    corrupt_zst.write_bytes(b"corrupt-zst-payload")
    corrupt_zst.chmod(0o600)

    # Both plain and .zst coexisting remain readable and deduplicated before recovery run.
    assert list_generations(root, "tab-crash") == sorted(gens)
    assert len(read_page(root, "tab-crash", gens[0])["events"]) == 1

    # 4. Non-overlapping lock: holding .compact.lock skips concurrent invocation cleanly.
    lock_file = root / ".compact.lock"
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked_res = compact_journals(root, keep_recent=1, min_age_seconds=60)
        assert locked_res["status"] == "skipped_locked" and locked_res["locked"] is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # 5. Graceful stop_event interruption stops cleanly without corrupting state.
    pre_stopped = threading.Event()
    pre_stopped.set()
    interrupted_res = compact_journals(root, keep_recent=1, min_age_seconds=60, stop_event=pre_stopped)
    assert interrupted_res["status"] == "interrupted" and interrupted_res["interrupted"] is True

    # 6. Full recovery compaction cleans orphan, resumes gens[0] unlink, repairs gens[1], and compacts gens[2].
    recovered = compact_journals(root, keep_recent=1, min_age_seconds=60)
    assert recovered["status"] == "ok" and recovered["failed"] == 0
    assert recovered["orphans_cleaned"] == 1
    assert recovered["resumed_unlinks"] == 1
    assert recovered["compressed"] == 2
    assert not orphan.exists()
    for idx, g in enumerate(gens[:3]):
        assert not (tab_dir / g).exists() and (tab_dir / f"{g}.zst").exists()
        events = read_page(root, "tab-crash", g)["events"]
        assert bytes.fromhex(events[0]["data_hex"]) == f"crash-stage-{idx}-".encode() * 96
    health = audit_journal_health(root)
    assert health["orphan_temp_files"] == 0
    assert health["generations_compressed"] == 3 and health["generations_plain"] == 1
    assert health["last_compaction"]["status"] == "ok"


def test_compaction_systemd_units_isolate_reserved_termdeck_cpus(tmp_path):
    from termdeck.output_journal import render_compaction_systemd_units
    repo_root = Path(__file__).resolve().parents[1]
    svc_file = (repo_root / "deploy/systemd/termdeck-journal-compaction.service").read_text()
    tmr_file = (repo_root / "deploy/systemd/termdeck-journal-compaction.timer").read_text()
    rendered_svc, rendered_tmr = render_compaction_systemd_units(
        tmp_path / "launch.py", tmp_path / "durability"
    )
    for unit_text in (svc_file, rendered_svc):
        assert "Nice=19" in unit_text
        assert "IOSchedulingClass=idle" in unit_text
        assert "CPUQuota=50%" in unit_text
        assert "AllowedCPUs=0-2,7-26,31-47" in unit_text
        # Verify none of TermDeck's 8 reserved logical CPUs (3-6, 27-30) are in AllowedCPUs.
        mask = next(l.split("=", 1)[1] for l in unit_text.splitlines() if l.startswith("AllowedCPUs="))
        cpus = set()
        for part in mask.split(","):
            lo, _, hi = part.partition("-")
            cpus.update(range(int(lo), int(hi or lo) + 1))
        assert cpus.isdisjoint({3, 4, 5, 6, 27, 28, 29, 30})
    for tmr_text in (tmr_file, rendered_tmr):
        assert "OnCalendar=Sun" in tmr_text and "Persistent=true" in tmr_text


def test_rust_recorder_sigkill_and_4mib_overflow_reattachment_with_gap_audit(tmp_path, monkeypatch):
    import fcntl
    import shutil
    import time
    from termdeck.directory_tracking import maintain_recorders
    from termdeck.output_journal import audit_journal_health, list_generations
    from termdeck.session_store import SessionStore
    from termdeck.startup_publication import prepare
    from termdeck.tmux_backend import TmuxBackend

    repo_root = Path(__file__).resolve().parents[1]
    rust_bin = repo_root / "web/target/release/termdeck-record-output"
    if not rust_bin.exists():
        rust_bin = repo_root / "web/target/debug/termdeck-record-output"
    assert rust_bin.exists(), "compiled termdeck-record-output binary required"

    # Build a verified immutable bundle containing current termdeck Python + compiled Rust recorder.
    src_copy = tmp_path / "src"
    shutil.copytree(repo_root / "termdeck", src_copy / "termdeck")
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o700)
    bundle = Path(prepare(src_copy, managed, recorder_binary=rust_bin)["bundle"])
    launcher = bundle / "launch.py"

    runtime = tmp_path / "tmux"
    runtime.mkdir(mode=0o700)
    for key in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))

    env = dict(os.environ)
    def tmux(*args):
        return subprocess.check_output(["/usr/bin/tmux", *args], env=env, text=True, timeout=5).strip()
    def rss_kib(pid):
        return next(int(l.split()[1]) for l in Path(f"/proc/{pid}/status").read_text().splitlines()
                    if l.startswith("VmRSS:"))

    db_path = tmp_path / "sessions.sqlite3"
    durability = tmp_path / "durability"
    store = SessionStore(db_path, provision_auth_scopes=False)
    record = store.create(name="fault-tab", tag="test", cwd=str(tmp_path), command="/bin/bash --noprofile --norc")
    store.update(record.id, status="running")
    backend = TmuxBackend(pane_cache_ttl=0)
    try:
        tmux("new-session", "-d", "-s", record.tmux_name, "-x", "90", "-y", "26", "/bin/bash --noprofile --norc")
        pane_info = tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_id} #{pane_pid}").split()
        orig_pane_pid = int(pane_info[1])
        server_pid = int(tmux("display-message", "-p", "#{pid}"))
        base_server_rss = rss_kib(server_pid)

        recorders = {}
        assert maintain_recorders(store, backend, durability, recorders, recorder_launcher=launcher) == 1

        owner_file = durability / "journals" / record.id / "pipe-owner.json"
        deadline = time.monotonic() + 8
        while not owner_file.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        import json
        owner1 = json.loads(owner_file.read_text())
        sink1_pid = int(owner1["pid"])
        gen1 = owner1["generation"]

        # Emit output BEFORE_SIGKILL and wait until flushed to gen1.
        tmux("send-keys", "-t", record.tmux_name, "-l", "echo BEFORE_SIGKILL_OK")
        tmux("send-keys", "-t", record.tmux_name, "Enter")
        deadline = time.monotonic() + 5
        while True:
            ev1 = b"".join(bytes.fromhex(e["data_hex"]) for e in read_page(durability, record.id, gen1)["events"])
            if b"BEFORE_SIGKILL_OK" in ev1:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)

        # Phase A: SIGKILL crash recovery & quantified unpiped gap.
        os.kill(sink1_pid, signal.SIGKILL)
        # Emit overwritten transient carriage-return bytes + visible SCROLLBACK_GAP_RECOVERED:
        # this output also triggers EPIPE in tmux so #{pane_pipe} transitions to 0.
        tmux("send-keys", "-t", record.tmux_name, "-l", "printf 'TRANSIENT_''OVERWRITTEN\\rSCROLLBACK_GAP_RECOVERED\\n'")
        tmux("send-keys", "-t", record.tmux_name, "Enter")
        deadline = time.monotonic() + 5
        while tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pipe}") != "0":
            assert time.monotonic() < deadline
            time.sleep(0.02)

        # Advance retry cooldown and trigger automatic reattachment.
        backend._recorder_attempts[record.id] = -120.0
        assert maintain_recorders(store, backend, durability, recorders, recorder_launcher=launcher) == 1
        deadline = time.monotonic() + 8
        while json.loads(owner_file.read_text())["generation"] == gen1:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        owner2 = json.loads(owner_file.read_text())
        gen2 = owner2["generation"]
        sink2_pid = int(owner2["pid"])
        assert int(tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pid}")) == orig_pane_pid

        ev2_init = b"".join(bytes.fromhex(e["data_hex"]) for e in read_page(durability, record.id, gen2)["events"])
        assert b"SCROLLBACK_GAP_RECOVERED" in ev2_init
        assert b"TRANSIENT_OVERWRITTEN" not in ev2_init  # Quantified raw-stream gap on overwritten \r during dead-sink window.

        # Phase B: 4 MiB backlog overflow under stalled disk lock + automatic reattachment.
        tmux("send-keys", "-t", record.tmux_name, "-l", "echo COMMITTED_BEFORE_OVERFLOW")
        tmux("send-keys", "-t", record.tmux_name, "Enter")
        deadline = time.monotonic() + 5
        while b"COMMITTED_BEFORE_OVERFLOW" not in b"".join(
            bytes.fromhex(e["data_hex"]) for e in read_page(durability, record.id, gen2)["events"]
        ):
            assert time.monotonic() < deadline
            time.sleep(0.02)

        copy_lock_fd = os.open(durability / ".journal-copy.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        peak_rss_delta = 0
        try:
            fcntl.flock(copy_lock_fd, fcntl.LOCK_EX)
            tmux("send-keys", "-t", record.tmux_name, "-l", "head -c 6291456 /dev/zero | tr '\\000' Z; echo AFTER_OVERFLOW_ALIVE")
            tmux("send-keys", "-t", record.tmux_name, "Enter")
            deadline = time.monotonic() + 10
            while tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pipe}") != "0":
                peak_rss_delta = max(peak_rss_delta, rss_kib(server_pid) - base_server_rss)
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert peak_rss_delta < 64 * 1024
            exit_deadline = time.monotonic() + 2
            while Path(f"/proc/{sink2_pid}").exists():
                assert time.monotonic() < exit_deadline
                time.sleep(0.02)
        finally:
            fcntl.flock(copy_lock_fd, fcntl.LOCK_UN)
            os.close(copy_lock_fd)

        # Verify pane survived overflow and structured failure receipt was persisted.
        deadline = time.monotonic() + 5
        while "AFTER_OVERFLOW_ALIVE" not in tmux("capture-pane", "-p", "-t", record.tmux_name):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert int(tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pid}")) == orig_pane_pid
        fail_notice = json.loads((durability / "journals" / record.id / f"{owner2['token']}.failed.json").read_text())
        assert fail_notice["reason"] == "backlog_overflow"

        # Reattach Gen 3 after overflow and audit health metrics.
        backend._recorder_attempts[record.id] = -120.0
        assert maintain_recorders(store, backend, durability, recorders, recorder_launcher=launcher) == 1
        deadline = time.monotonic() + 8
        while json.loads(owner_file.read_text())["generation"] == gen2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        gen3 = json.loads(owner_file.read_text())["generation"]
        assert len(list_generations(durability, record.id)) == 3

        health = audit_journal_health(durability)
        assert health["recorder_failures_total"] == 1
        assert health["failures_by_reason"] == {"backlog_overflow": 1}
        assert health["reattachments_total"] == 2
        assert health["recording_gaps_total"] == 2
        reasons = [g["prior_failure_reason"] for g in health["recent_gaps"]]
        assert set(reasons) == {"unclean_exit_or_sigkill", "backlog_overflow"}
        assert gen3 != gen2
    finally:
        store.close()
        subprocess.run(["/usr/bin/tmux", "kill-server"], env=env, capture_output=True, timeout=5)


from contextlib import contextmanager
@contextmanager
def mock_argv(fake_argv, owner_payload, tab_dir):
    import json
    import termdeck.resources as res
    from unittest import mock
    owner_payload["argv"] = fake_argv
    (tab_dir / "pipe-owner.json").write_text(json.dumps(owner_payload))
    (tab_dir / "pipe-owner.json").chmod(0o600)
    with mock.patch.object(res, "_process_argv", return_value=tuple(fake_argv)):
        yield


def test_mirror_journals_incremental_suffix_and_zst_awareness(tmp_path):
    from contextlib import closing
    from termdeck.output_journal import OutputJournal, compact_journals, mirror_journals, read_page

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst2 = tmp_path / "dst2"

    # Create an older generation and an active generation on source.
    with closing(OutputJournal(src, "tab-inc")) as old_j:
        old_j.append(b"older-generation-payload\n" * 64, geometry=(80, 24))
        old_gen = old_j.path.name

    active = OutputJournal(src, "tab-inc")
    try:
        active.append(b"chunk-one\n" * 50, geometry=(80, 24))
        first = mirror_journals(src, dst)
        assert first["copied"] == 2 and first["skipped"] == 0
        active_gen = active.path.name
        initial_active_size = (dst / "journals" / "tab-inc" / active_gen).stat().st_size

        # Append delta to active generation -> mirror transfers ONLY the newly committed suffix!
        active.append(b"chunk-two-delta\n" * 20)
        second = mirror_journals(src, dst)
        assert second["copied"] == 1 and second["skipped"] == 1
        new_active_size = (dst / "journals" / "tab-inc" / active_gen).stat().st_size
        assert second["bytes"] == new_active_size - initial_active_size
        assert second["bytes"] < new_active_size

        # Simulate an interrupted/uncommitted trailing tail on dst beyond .commit -> truncated cleanly on next append.
        with open(dst / "journals" / "tab-inc" / active_gen, "ab") as f:
            f.write(b"UNCOMMITTED_CRASH_TAIL")
        active.append(b"chunk-three\n" * 10)
        third = mirror_journals(src, dst)
        assert third["copied"] == 1
        replayed = b"".join(
            bytes.fromhex(e["data_hex"]) for e in read_page(dst, "tab-inc", active_gen)["events"]
        )
        assert b"chunk-one\n" in replayed and b"chunk-two-delta\n" in replayed and b"chunk-three\n" in replayed
        assert b"UNCOMMITTED_CRASH_TAIL" not in replayed
    finally:
        active.close()

    # Compact older generation on dst -> subsequent mirror skips .tdj.zst without re-copying plain .tdj!
    comp = compact_journals(dst, keep_recent=1, min_age_seconds=0)
    assert comp["compressed"] == 1
    assert not (dst / "journals" / "tab-inc" / old_gen).exists()
    assert (dst / "journals" / "tab-inc" / f"{old_gen}.zst").exists()
    fourth = mirror_journals(src, dst)
    assert fourth["copied"] == 0 and fourth["skipped"] == 2
    assert not (dst / "journals" / "tab-inc" / old_gen).exists()

    # Compact older generation on src too -> mirroring to a brand-new dst2 replicates the .tdj.zst archive!
    assert compact_journals(src, keep_recent=1, min_age_seconds=0)["compressed"] == 1
    fifth = mirror_journals(src, dst2)
    assert fifth["copied"] == 2
    assert (dst2 / "journals" / "tab-inc" / f"{old_gen}.zst").exists()
    old_events = b"".join(
        bytes.fromhex(e["data_hex"]) for e in read_page(dst2, "tab-inc", old_gen)["events"]
    )
    assert old_events == b"older-generation-payload\n" * 64

