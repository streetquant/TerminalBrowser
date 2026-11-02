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
