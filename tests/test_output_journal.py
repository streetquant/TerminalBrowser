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
