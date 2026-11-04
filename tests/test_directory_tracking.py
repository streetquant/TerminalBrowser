"""Directory recovery must work without a GTK window or a browser refresh."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

from termdeck.directory_tracking import install, save_directories
from termdeck.session_recipe import recipe_from_record
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend


def test_shell_directory_updates_recipe_and_preserves_layout_and_manual_name(tmp_path):
    original, changed = tmp_path / "original", tmp_path / "changed"
    original.mkdir(); changed.mkdir()
    with_store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    try:
        record = with_store.create(name="My tab", tag="test", cwd=str(original), command="/bin/bash")
        record = with_store.update(record.id, status="running", automatic_name=False,
                                  recovery_cwd=str(original), window_id="saved-window", tab_index=9)
        backend = SimpleNamespace(current_directories=lambda _: {record.id: str(changed)})
        assert save_directories(with_store, backend) == {"selected": 1, "updated": 1, "failed": 0}
        updated = with_store.get(record.id)
        assert updated.cwd == recipe_from_record(updated).cwd == str(changed)
        assert updated.recovery_cwd is None
        assert (updated.name, updated.window_id, updated.tab_index, updated.last_opened_at) == (
            record.name, record.window_id, record.tab_index, record.last_opened_at)
        assert save_directories(with_store, backend)["updated"] == 0
    finally:
        with_store.close()


def test_missing_pane_and_concurrent_lifecycle_change_preserve_saved_directory(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    try:
        record = store.create(name="tab", tag="test", cwd=str(tmp_path), command="/bin/bash")
        record = store.update(record.id, status="running")
        assert save_directories(store, SimpleNamespace(current_directories=lambda _: {}))["updated"] == 0

        def changed(_):
            store.update(record.id, status="stopped", name="Renamed")
            return {record.id: "/"}

        assert save_directories(store, SimpleNamespace(current_directories=changed))["updated"] == 0
        current = store.get(record.id)
        assert (current.status, current.name, current.cwd) == ("stopped", "Renamed", str(tmp_path))
    finally:
        store.close()


def test_unavailable_saved_mount_is_not_replaced_by_reboot_fallback(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    try:
        intended = str(tmp_path / "unmounted-project")
        record = store.create(name="project", tag="test", cwd=intended, command="/bin/bash")
        record = store.update(record.id, status="running")
        backend = SimpleNamespace(current_directories=lambda _: {record.id: str(tmp_path)})
        assert save_directories(store, backend)["updated"] == 0
        assert recipe_from_record(store.get(record.id)).cwd == intended
    finally:
        store.close()


def test_real_tmux_directory_survives_backend_loss(tmp_path, monkeypatch):
    runtime = tmp_path / "tmux"
    runtime.mkdir(mode=0o700)
    for key in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))
    original, changed = tmp_path / "original", tmp_path / "space and 'quote"
    original.mkdir(); changed.mkdir()
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    record = store.create(name="saved", tag="test", cwd=str(original), command="/bin/bash")
    record = store.update(record.id, status="running")
    def tmux(*args):
        return subprocess.check_output(("/usr/bin/tmux", *args), text=True, timeout=5).strip()
    try:
        tmux("new-session", "-d", "-s", record.tmux_name, "-c", str(changed), "/bin/bash")
        assert save_directories(store, TmuxBackend(pane_cache_ttl=0))["updated"] == 1
        tmux("kill-server")  # This test's private socket only.
        saved = store.get(record.id)
        TmuxBackend().create_or_resume(saved)
        assert tmux("display-message", "-p", "-t", saved.tmux_name, "#{pane_current_path}") == str(changed)
        assert recipe_from_record(saved).cwd == str(changed)
    finally:
        subprocess.run(("/usr/bin/tmux", "kill-server"), capture_output=True, timeout=5)
        store.close()


def test_directory_service_uses_verified_immutable_payload_and_preserves_web(tmp_path):
    source = tmp_path / "source"
    package = source / "termdeck"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "startup_recovery.py").write_text("def main(): return 0\n")
    managed, state, units = (tmp_path / name for name in ("managed", "state", "units"))
    for directory in (managed, state, units):
        directory.mkdir(mode=0o700)
    web = units / "termdeck-web-backend.service"
    web.write_text("unchanged\n")
    result = install(source, managed, state, units, tmp_path / "backup.json")
    unit = Path(result["unit_path"]).read_text()
    assert '"--track-directories"' in unit and "Type=simple" in unit
    assert "Restart=on-failure" in unit and "RemainAfterExit" not in unit
    assert "WantedBy=termdeck-web-backend.service" in unit
    assert web.read_text() == "unchanged\n"
    subprocess.run(("/usr/bin/python3", "-I", "-B", str(Path(result["bundle"]) / "launch.py"),
                    "--verify-only"), check=True)


def test_headless_recording_survives_observer_sigkill_without_app_loss(tmp_path, monkeypatch):
    import json
    import os
    import signal
    import sys
    import time
    from termdeck.output_journal import read_page
    from termdeck.startup_publication import prepare
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o700)
    launcher = Path(prepare(Path(__file__).resolve().parents[1], managed)["bundle"]) / "launch.py"
    runtime = tmp_path / "tmux"
    runtime.mkdir(mode=0o700)
    for key in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    record = store.create(name="saved", tag="test", cwd=str(tmp_path), command="/bin/bash")
    store.update(record.id, status="running")
    def tmux(*args):
        return subprocess.check_output(("/usr/bin/tmux", *args), text=True, timeout=5).strip()
    child = None
    try:
        tmux("new-session", "-d", "-s", record.tmux_name, "-x", "93", "-y", "29", "/bin/bash")
        original = tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pid} #{pane_width} #{pane_height}")
        script = '''
from pathlib import Path
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend
from termdeck.directory_tracking import maintain_recorders
import sys,time
store=SessionStore(Path(sys.argv[1]), provision_auth_scopes=False)
recorders={}
maintain_recorders(store,TmuxBackend(pane_cache_ttl=0),Path(sys.argv[2]),recorders,recorder_launcher=Path(sys.argv[3]))
while not all(c._ready for c,j,p in recorders.values()): time.sleep(.01)
print('recording',flush=True)
while True: time.sleep(1)
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(store.path), str(tmp_path / "durability"), str(launcher)],
            stdout=subprocess.PIPE, text=True)
        # Bound setup without blocking on an unreadable pipe forever.
        import select
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == "recording"
        assert tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pid} #{pane_width} #{pane_height}") == original
        marker = "RECORDER_PROOF_" + os.urandom(12).hex()
        tmux("send-keys", "-t", record.tmux_name, "-l", "printf '\\n" + marker + "\\n'")
        tmux("send-keys", "-t", record.tmux_name, "Enter")
        deadline = time.monotonic() + 10
        while True:
            page = read_page(tmp_path / "durability", record.id)
            if marker.encode() in b"".join(bytes.fromhex(e["data_hex"]) for e in page["events"]):
                break
            assert time.monotonic() < deadline
            time.sleep(.02)
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        assert tmux("display-message", "-p", "-t", record.tmux_name, "#{pane_pid} #{pane_width} #{pane_height}") == original
        after = "AFTER_OBSERVER_KILL_" + os.urandom(12).hex()
        escaped = "".join(f"\\x{byte:02x}" for byte in after.encode())
        tmux("send-keys", "-t", record.tmux_name, "-l", "printf '" + escaped + "\\n'")
        tmux("send-keys", "-t", record.tmux_name, "Enter")
        deadline = time.monotonic() + 5
        while after.encode() not in b"".join(bytes.fromhex(e["data_hex"]) for e in read_page(tmp_path / "durability", record.id)["events"]):
            assert time.monotonic() < deadline
            time.sleep(.01)
        # Losing the application/server afterward still leaves its bytes.
        tmux("kill-server")
        page = read_page(tmp_path / "durability", record.id)
        assert marker.encode() in b"".join(bytes.fromhex(e["data_hex"]) for e in page["events"])
        assert after.encode() in b"".join(bytes.fromhex(e["data_hex"]) for e in page["events"])
    finally:
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=5)
        subprocess.run(("/usr/bin/tmux", "kill-server"), capture_output=True, timeout=5)
        store.close()


def test_transient_inventory_failure_keeps_existing_recorders(tmp_path):
    from termdeck.directory_tracking import maintain_recorders
    from unittest import mock
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    try:
        record = store.create(name="tab",tag="test",cwd=str(tmp_path),command="/bin/bash")
        store.update(record.id,status="running")
        controller, journal = mock.Mock(), mock.Mock()
        existing = {record.id:(controller,journal,123)}
        backend = SimpleNamespace(_run=lambda *_a, **_kw: SimpleNamespace(returncode=124))
        assert maintain_recorders(store,backend,tmp_path/'durability',existing)==1
        controller.stop.assert_not_called(); journal.close.assert_not_called()
        assert existing[record.id][2]==123
    finally:
        store.close()


def test_fleet_shutdown_has_one_shared_deadline():
    from termdeck.directory_tracking import stop_recorders
    import threading,time
    release=threading.Event()
    controller=SimpleNamespace(stop=lambda: release.wait(1))
    journal=SimpleNamespace(close=lambda: None)
    start=time.monotonic()
    try:
        remaining=stop_recorders({str(i):(controller,journal,i) for i in range(293)},timeout=.05)
        assert remaining and time.monotonic()-start<.5
    finally:
        release.set()


def test_background_native_history_survives_idle_shell_and_rejects_stale_observation(tmp_path):
    import json
    from termdeck.directory_tracking import save_recovery_metadata
    from termdeck.recovery_authority_context import RecoveryObservationBatch
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    root = tmp_path / "durability"
    try:
        record = store.create(name="app", tag="test", cwd=str(tmp_path), command="/bin/bash")
        store.update(record.id, status="running")
        command = "omp --resume /saved/exact-native-session.jsonl"
        def observe(_records, **kwargs):
            assert kwargs == {"native_capture": True}
            return RecoveryObservationBatch({record.id: (command, str(tmp_path))}, {})
        backend = SimpleNamespace(recovery_observations=observe)
        assert save_recovery_metadata(store, backend, root) == 1
        history = list((root / "applications" / record.id).glob("*.json"))
        assert len(history) == 1
        assert json.loads(history[0].read_text())["command"] == command
        assert save_recovery_metadata(store, backend, root) == 0
        assert len(list(history[0].parent.glob("*.json"))) == 1
        backend.recovery_observations = lambda *_a, **_kw: RecoveryObservationBatch({record.id: (None, None)}, {})
        assert save_recovery_metadata(store, backend, root) == 1
        assert store.get(record.id).recovery_command is None
        assert history[0].exists()
        def concurrent_intent(*_a, **_kw):
            store.update(record.id, status="stopped", name="User stopped this tab")
            return RecoveryObservationBatch({record.id: (command, str(tmp_path))}, {})
        backend.recovery_observations = concurrent_intent
        assert save_recovery_metadata(store, backend, root) == 0
        assert store.get(record.id).status == "stopped"
        assert store.get(record.id).recovery_command is None
        assert len(list(history[0].parent.glob("*.json"))) == 1
        from termdeck.recovery_authority_context import AuthorityObservation, record_cas
        store.update(record.id, status="running")
        current = store.get(record.id)
        backend.recovery_observations = lambda *_a, **_kw: RecoveryObservationBatch(
            {record.id: (command, str(tmp_path))},
            {record.id: AuthorityObservation(record_cas(current), None, None, is_current=lambda: False)})
        assert save_recovery_metadata(store, backend, root) == 0
        assert store.get(record.id).recovery_command is None
        assert len(list(history[0].parent.glob("*.json"))) == 1
        checks = []
        def changes_at_write():
            checks.append(True)
            return len(checks) == 1
        backend.recovery_observations = lambda *_a, **_kw: RecoveryObservationBatch(
            {record.id: (command, str(tmp_path))},
            {record.id: AuthorityObservation(record_cas(current), None, None, is_current=changes_at_write)})
        assert save_recovery_metadata(store, backend, root) == 0
        assert len(checks) == 2
        assert store.get(record.id).recovery_command is None
    finally:
        store.close()


def test_native_recipe_not_published_when_history_cannot_be_flushed(tmp_path, monkeypatch):
    from termdeck.directory_tracking import save_recovery_metadata
    from termdeck.recovery_authority_context import RecoveryObservationBatch
    import pytest
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    try:
        record = store.create(name="app", tag="test", cwd=str(tmp_path), command="/bin/bash")
        store.update(record.id, status="running")
        backend = SimpleNamespace(recovery_observations=lambda *_a, **_kw:
            RecoveryObservationBatch({record.id: ("omp --resume /saved/session.jsonl", str(tmp_path))}, {}))
        def fail(*_args):
            raise OSError("disk full")
        monkeypatch.setattr("termdeck.session_durability._atomic", fail)
        with pytest.raises(OSError, match="disk full"):
            save_recovery_metadata(store, backend, tmp_path / "durability")
        assert store.get(record.id).recovery_command is None
        assert not store.conn.in_transaction
    finally:
        store.close()


def test_pipe_restart_preserves_user_pipe_and_live_writer_lease(tmp_path, monkeypatch):
    import os, shlex, signal, time
    from termdeck.directory_tracking import maintain_recorders
    from termdeck.output_journal import pipe_owner, read_page
    from termdeck.resources import _parse_stat, _process_argv
    from termdeck.startup_publication import prepare
    runtime, managed = tmp_path / 'tmux', tmp_path / 'managed'
    runtime.mkdir(mode=0o700); managed.mkdir(mode=0o700)
    for key in ('TERMDECK_STATE_ROOT', 'T1000_MANAGED_ROOT', 'TERMDECK_TMUX_RUNTIME', 'TERMDECK_TMUX_IDENTITY', 'TMUX', 'TMUX_PANE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('TMUX_TMPDIR', str(runtime))
    launcher = Path(prepare(Path(__file__).resolve().parents[1], managed)['bundle']) / 'launch.py'
    root = tmp_path / 'durability'
    store = SessionStore(tmp_path / 'sessions.sqlite3', provision_auth_scopes=False)
    def tmux(*args):
        return subprocess.check_output(('/usr/bin/tmux', *args), text=True, timeout=5).strip()
    def wait_for(predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(.01)
    def output(record, marker):
        escaped = ''.join(f'\\x{byte:02x}' for byte in marker.encode())
        tmux('send-keys', '-t', record.tmux_name, '-l', "printf '" + escaped + "\\n'")
        tmux('send-keys', '-t', record.tmux_name, 'Enter')
    own = store.create(name='owned', tag='test', cwd=str(tmp_path), command='/bin/bash --noprofile --norc')
    user = store.create(name='user pipe', tag='test', cwd=str(tmp_path), command='/bin/bash --noprofile --norc')
    stopped_sink = None
    try:
        for record in (own, user):
            store.update(record.id, status='running')
            tmux('new-session', '-d', '-s', record.tmux_name, '-x', '91', '-y', '27', record.command)
        identities = {r.id: tmux('display-message', '-p', '-t', r.tmux_name, '#{pane_pid} #{pane_width} #{pane_height}') for r in (own, user)}
        user_log = tmp_path / 'user-output'
        tmux('pipe-pane', '-o', '-O', '-t', user.tmux_name, 'exec /usr/bin/cat >> ' + shlex.quote(str(user_log)))
        server_pid = int(tmux('display-message', '-p', '#{pid}'))
        wait_for(lambda: user_log.exists())
        children = Path(f'/proc/{server_pid}/task/{server_pid}/children').read_text().split()
        user_pid = next(int(pid) for pid in children if _process_argv(int(pid)) == ('/usr/bin/cat',))
        user_start = _parse_stat(user_pid)['starttime']
        # Burst while the pipe and initial snapshot are installed. Exact
        # markers are produced by the app, not present in its echoed command.
        tmux('send-keys', '-t', own.tmux_name, '-l',
             "for ((i=0;i<100;i++)); do printf 'ATTACH_BURST_%04d\\n' \"$i\"; sleep .002; done")
        tmux('send-keys', '-t', own.tmux_name, 'Enter')
        recorders = {}
        maintain_recorders(store, TmuxBackend(), root, recorders, recorder_launcher=launcher)
        assert user.id not in recorders and own.id in recorders
        wait_for(lambda: recorders[own.id][0]._ready)
        observer = recorders[own.id][0]
        owner = pipe_owner(root, own.id, observer.pane_id, server_pid, observer.server_start)
        assert owner is not None
        def burst_present():
            raw = b''.join(bytes.fromhex(e['data_hex']) for e in read_page(root, own.id)['events'])
            positions = [raw.find(f'ATTACH_BURST_{i:04d}'.encode()) for i in range(100)]
            return all(p >= 0 for p in positions) and positions == sorted(positions)
        wait_for(burst_present)
        output(own, 'OWNED_OUTPUT_BEFORE_RESTART')
        output(user, 'USER_PIPE_BEFORE_RESTART')
        wait_for(lambda: b'OWNED_OUTPUT_BEFORE_RESTART' in b''.join(bytes.fromhex(e['data_hex']) for e in read_page(root, own.id)['events']))
        wait_for(lambda: 'USER_PIPE_BEFORE_RESTART' in user_log.read_text())
        fresh = {}
        maintain_recorders(store, TmuxBackend(), root, fresh, recorder_launcher=launcher)
        restarted = pipe_owner(root, own.id, observer.pane_id, server_pid, observer.server_start)
        assert restarted['pid'] == owner['pid'] and restarted['generation'] == owner['generation']
        assert len(list((root / 'journals' / own.id).glob('*.tdj'))) == 1
        output(user, 'USER_PIPE_AFTER_RESTART')
        wait_for(lambda: 'USER_PIPE_AFTER_RESTART' in user_log.read_text())
        assert _parse_stat(user_pid)['starttime'] == user_start
        # A tmux pipe belongs to the stable pane, across an explicit respawn.
        # Only this disposable application is replaced.
        tmux('respawn-pane', '-k', '-t', own.tmux_name, own.command)
        new_identity = tmux('display-message', '-p', '-t', own.tmux_name, '#{pane_pid} #{pane_width} #{pane_height}')
        assert new_identity != identities[own.id]
        identities[own.id] = new_identity
        maintain_recorders(store, TmuxBackend(), root, fresh, recorder_launcher=launcher)
        after_respawn = pipe_owner(root, own.id, observer.pane_id, server_pid, observer.server_start)
        assert after_respawn['pid'] == owner['pid'] and after_respawn['generation'] == owner['generation']
        import json
        binding = json.loads((root / 'journals' / own.id / 'pane-current.json').read_text())
        assert binding['pid'] == int(new_identity.split()[0])
        tmux('resize-window', '-t', own.tmux_name, '-x', '97', '-y', '29')
        output(own, 'OUTPUT_AFTER_RESPAWN_AND_RESIZE')
        wait_for(lambda: any(e['kind'] == 2 and e['cols'] == 97 and e['rows'] == 29
                            for e in read_page(root, own.id)['events']))
        wait_for(lambda: b'OUTPUT_AFTER_RESPAWN_AND_RESIZE' in b''.join(bytes.fromhex(e['data_hex']) for e in read_page(root, own.id)['events']))
        identities[own.id] = tmux('display-message', '-p', '-t', own.tmux_name, '#{pane_pid} #{pane_width} #{pane_height}')
        # A still-live previous sink retains the lifetime lease even when its
        # pipe is closed. A second attempt cannot publish a new generation.
        stopped_sink = owner['pid']
        os.kill(stopped_sink, signal.SIGSTOP)
        tmux('pipe-pane', '-t', own.tmux_name)  # Proven owned, disposable pipe only.
        maintain_recorders(store, TmuxBackend(), root, {}, recorder_launcher=launcher)
        wait_for(lambda: bool(list((root / 'journals' / own.id).glob('*.failed.json'))))
        assert len(list((root / 'journals' / own.id).glob('*.tdj'))) == 1
        assert read_page(root, own.id)['generation'] == owner['generation']
        os.kill(stopped_sink, signal.SIGCONT); stopped_sink = None
        wait_for(lambda: _process_argv(owner['pid']) is None)
        output(own, 'TRIGGER_CLOSED_PIPE_EOF')
        wait_for(lambda: tmux('display-message', '-p', '-t', own.tmux_name, '#{pane_pipe}') == '0')
        replacement = {}
        maintain_recorders(store, TmuxBackend(), root, replacement, recorder_launcher=launcher)
        wait_for(lambda: replacement[own.id][0]._ready)
        assert read_page(root, own.id)['generation'] != owner['generation']
        assert b'OWNED_OUTPUT_BEFORE_RESTART' in b''.join(bytes.fromhex(e['data_hex']) for e in read_page(root, own.id, owner['generation'])['events'])
        # A recording storage failure detaches its sink, not the application.
        directory = root / 'journals' / own.id
        directory.chmod(0o500)
        try:
            output(own, 'APPLICATION_ALIVE_DURING_DISK_FAILURE')
            wait_for(lambda: not replacement[own.id][0]._ready)
            assert 'APPLICATION_ALIVE_DURING_DISK_FAILURE' in tmux('capture-pane', '-p', '-t', own.tmux_name)
        finally:
            directory.chmod(0o700)
        for record in (own, user):
            assert tmux('display-message', '-p', '-t', record.tmux_name, '#{pane_pid} #{pane_width} #{pane_height}') == identities[record.id]
        assert _parse_stat(user_pid)['starttime'] == user_start
    finally:
        if stopped_sink is not None:
            os.kill(stopped_sink, signal.SIGCONT)
        subprocess.run(('/usr/bin/tmux', 'kill-server'), capture_output=True, timeout=5)
        store.close()
