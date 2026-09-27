"""Host terminal environment, with all live process checks in owned tmux sockets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from termdeck import storage_fabric as storage
from termdeck.models import SessionRecord
from termdeck.session_recipe import record_recipe_fields
from termdeck.tmux_backend import TmuxBackend, TmuxError


ROOT = Path(__file__).resolve().parents[1]
HOST_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "PATH")


def record(cwd, *, command="/bin/bash", tag="terminal", environment=None, **extra):
    encoded, digest, policy = record_recipe_fields(
        command=command, cwd=str(cwd), environment=environment,
        restart_policy="on-failure",
    )
    return SessionRecord(
        id="td-native-env", name="native-env", tag=tag, command=command,
        cwd=str(cwd), tmux_name="termdeck_native_env", created_at="now",
        last_opened_at="now", status="new", recovery_recipe=encoded,
        recovery_recipe_sha256=digest, restart_policy=policy, **extra,
    )


def backend():
    checkpoints = SimpleNamespace(
        restart_command=lambda _record: None,
        wrap_launch_command=lambda _record, command: command,
    )
    return TmuxBackend(executable="/usr/bin/tmux", checkpoint_manager=checkpoints,
                       command_timeout=5, query_timeout=2)


@pytest.fixture
def admitted_configured_entrypoint(monkeypatch):
    # These tests exercise shell/environment construction with an inert payload.
    # The real plan, actor, attachment and permit matrix lives in agent_launch.
    monkeypatch.setattr("termdeck.agent_launch.validate_native_launch", lambda _record: None)


def test_backend_service_inherits_host_identity_and_process_defaults():
    text = (ROOT / "deploy/systemd/termdeck-web-backend.service").read_text()
    assert "Environment=HOME=%h\n" in text
    assert "%h/.local/bin" in text
    assert "UMask=" not in text
    assert "LimitNOFILE=" not in text
    for restriction in ("NoNewPrivileges=", "ProtectHome=", "ProtectSystem="):
        assert restriction not in text
    recovery = (ROOT / "deploy/systemd/termdeck-web-recovery.service").read_text()
    assert "Environment=HOME=/warp/projects/softwares/termdeck/.termdeck/state\n" in recovery
    assert "NoNewPrivileges=yes" in recovery
    assert "ProtectSystem=strict" in recovery


@pytest.mark.parametrize("tag", ["terminal", "web-agent"])
def test_launch_refreshes_host_environment_and_preserves_explicit_recipe(tmp_path, monkeypatch, tag, admitted_configured_entrypoint):
    expected = dict(HOME=str(tmp_path), USER="fixture", LOGNAME="fixture",
                    SHELL="/bin/bash", PATH="/profile/bin:/usr/bin:/bin")
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    spec = backend()._launch_spec(record(tmp_path, tag=tag,
        environment={"USER": "recipe-user", "EXPLICIT": "kept"}))
    actual = dict(spec.environment)
    assert {key: actual.get(key) for key in HOST_KEYS} == {**expected, "USER": "recipe-user"}
    assert actual["EXPLICIT"] == "kept"


def test_private_path_preserves_ordinary_entries_and_recipe_path(tmp_path, monkeypatch, admitted_configured_entrypoint):
    private = tmp_path / "terminal-auth" / "auth-current"
    other = tmp_path / "terminal-auth" / "auth-other" / "bin"
    host_path = f":relative/bin:/user/npm/bin:{other}:/usr/bin:"
    monkeypatch.setenv("PATH", host_path)
    assert TmuxBackend._private_auth_path(private) == f"{private / 'bin'}::relative/bin:/user/npm/bin:/usr/bin:"
    spec = backend()._launch_spec(record(
        tmp_path, tag="web-agent", command="exec /usr/bin/true",
        codex_home=str(private), auth_scope_id="auth-current",
        environment={"PATH": "/recipe/tools:/usr/bin"},
    ))
    assert dict(spec.environment)["PATH"] == f"{private / 'bin'}:/recipe/tools:/usr/bin"


@pytest.mark.parametrize("cwd_kind", ["root", "alias", "group-writable"])
def test_native_recipe_accepts_accessible_host_directories(tmp_path, cwd_kind):
    target = tmp_path / "target"
    target.mkdir(mode=0o770)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    cwd = {"root": Path("/"), "alias": alias, "group-writable": target}[cwd_kind]
    assert backend()._launch_spec(record(cwd)).directory == cwd


@pytest.fixture
def private_tmux(monkeypatch):
    # An attested production-shaped routing binding, never the host's socket.
    with tempfile.TemporaryDirectory(prefix="td-host-env-") as temporary:
        root = Path(temporary)
        device = f"{os.major(root.stat().st_dev)}:{os.minor(root.stat().st_dev)}"
        monkeypatch.setattr(storage, "_PRODUCTION_TMUX_RUNTIME_BINDING", (root, device))
        monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(root))
        monkeypatch.setenv("TMUX_TMPDIR", str(root))
        monkeypatch.delenv("TMUX", raising=False)
        try:
            yield root
        finally:
            subprocess.run(["/usr/bin/tmux", "-S", str(root / f"tmux-{os.geteuid()}" / "default"),
                            "kill-server"], capture_output=True, timeout=5)
            binding = storage._PRODUCTION_TMUX_RUNTIME_BINDING
            if isinstance(binding, storage._ProductionDirectoryBinding):
                binding.close()


def wait_json(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.025)
    raise AssertionError(f"owned terminal did not publish its probe: {path}")


def wait_dead(tmux, target):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lifecycle = tmux._pane_lifecycle(target)
        if lifecycle is not None and lifecycle.dead:
            return lifecycle
        time.sleep(0.025)
    raise AssertionError("owned terminal did not finish its requested fixture exit")


def test_actual_tmux_refreshes_stale_server_home_path_and_preserves_host_permissions(private_tmux, monkeypatch):
    root = private_tmux
    stale, home, work, outside = (root / name for name in ("old-home", "home", "work", "outside"))
    for directory in (stale, home, work, outside):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(stale))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/bash")
    tmux = backend()
    tmux._run(["new-session", "-d", "-s", "owned_anchor", "/bin/sleep 120"])
    # Exercise ordinary tab creation/respawn even when CI has a private XDG
    # runtime with no user manager. Lifecycle acceptance is separately proven
    # by the real inherited-service gate; the blocked path is tested below.
    output = root / "observed.json"
    probe = root / "probe.py"
    probe.write_text(
        "import json, os, pathlib, sys\n"
        "status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)\n"
        "outside=pathlib.Path(sys.argv[2]); (outside/'written').write_text('host-write')\n"
        "value={'home':os.environ['HOME'],'path':os.environ['PATH'],'cwd':os.getcwd(),"
        "'rc':os.environ.get('TERMDECK_FIXTURE_RC'),'uid':os.getuid(),'gid':os.getgid(),"
        "'groups':os.getgroups(),'tty':os.isatty(0),'root_access':os.access('/',os.R_OK|os.X_OK),"
        "'mount':os.readlink('/proc/self/ns/mnt'),'status':{k:status[k].strip() for k in ['Umask','NoNewPrivs','Seccomp']}}\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(value))\n"
    )
    probe_command = shlex.join([sys.executable, str(probe), str(output), str(outside)])
    (home / ".bashrc").write_text(
        'export TERMDECK_FIXTURE_RC=loaded\nexport PATH="/profile/npm/bin:$PATH"\n'
        + probe_command + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/host/bin:/usr/bin:/bin")
    alias = root / "work-alias"
    alias.symlink_to(work, target_is_directory=True)
    tmux.create_or_resume(record(alias))
    observed = wait_json(output)
    host_status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert observed["home"] == str(home)
    assert observed["path"] == "/profile/npm/bin:/host/bin:/usr/bin:/bin"
    assert observed["cwd"] == str(work)
    assert observed["rc"] == "loaded"
    assert observed["uid"] == os.getuid() and observed["gid"] == os.getgid()
    assert observed["groups"] == os.getgroups()
    assert observed["tty"] is True and observed["root_access"] is True
    assert observed["mount"] == os.readlink("/proc/self/ns/mnt")
    assert observed["status"] == {key: host_status[key].strip() for key in ("Umask", "NoNewPrivs", "Seccomp")}
    assert (outside / "written").read_text() == "host-write"
    # Restart the same owned pane after a genuine shell failure. tmux keeps
    # separate session/server environments; both must yield to the new caller.
    tmux._run(["send-keys", "-t", "termdeck_native_env", "exit 7", "Enter"])
    lifecycle = wait_dead(tmux, record(alias))
    output.unlink()
    resumed_home = root / "resumed-home"
    resumed_home.mkdir()
    (resumed_home / ".bashrc").write_text((home / ".bashrc").read_text())
    monkeypatch.setenv("HOME", str(resumed_home))
    monkeypatch.setenv("PATH", "/resumed/tools:/usr/bin:/bin")
    assert tmux._respawn_dead_pane(record("/"), lifecycle)
    resumed = wait_json(output)
    assert resumed["home"] == str(resumed_home)
    assert resumed["path"] == "/profile/npm/bin:/resumed/tools:/usr/bin:/bin"
    assert resumed["cwd"] == "/" and resumed["tty"] is True
    assert resumed["status"] == observed["status"]
    # The canonical server's process-wide values were not rewritten to fix a tab.
    assert tmux._run(["show-environment", "-g", "HOME"]).stdout.strip() == f"HOME={stale}"
    assert tmux._run(["has-session", "-t", "owned_anchor"], check=False).returncode == 0


def test_unavailable_private_user_manager_preserves_own_server_and_neighbor(private_tmux, monkeypatch):
    from termdeck.tmux_server_lifecycle import user_systemd_scope_available

    unavailable = private_tmux / "unavailable-user-manager"
    unavailable.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(unavailable))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + str(unavailable / "bus"))
    assert not user_systemd_scope_available()
    tmux = backend()
    for name in ("owned_anchor", "owned_neighbor"):
        tmux._run(["new-session", "-d", "-s", name, "exec /bin/sleep 120"])

    def snapshot():
        pid = int(tmux._run(["display-message", "-p", "#{pid}"]).stdout.strip())
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        sessions = tmux._run(["list-sessions", "-F", "#{session_name}|#{session_id}|#{session_created}"]).stdout
        panes = tmux._run(["list-panes", "-a", "-F", "#{session_name}|#{pane_id}|#{pane_pid}|#{pane_dead}"]).stdout
        return pid, int(fields[19]), (proc / "cgroup").read_text().strip(), sessions, panes

    before = snapshot()
    assert "owned_anchor|" in before[3] and "owned_neighbor|" in before[3]
    refusal = tmux.ensure_server_lifecycle_isolated()
    assert not refusal.ok and refusal.server_present and not refusal.changed
    assert refusal.detail.startswith("recovery-blocked: user systemd is unavailable")
    assert (refusal.server_pid, refusal.server_start_ticks, refusal.cgroup) == before[:3]
    assert snapshot() == before
    assert tmux.ensure_server_lifecycle_isolated() == refusal
    assert snapshot() == before


@pytest.mark.parametrize("private_scope", [False, True])
def test_configured_entrypoint_receives_login_profile_without_implicit_fallback(private_tmux, monkeypatch, private_scope, admitted_configured_entrypoint):
    root = private_tmux
    home = root / "home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        'export TERMDECK_FIXTURE_PROFILE=loaded\nexport PATH="/profile/npm/bin:$PATH"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/base/bin:/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/bash")
    output = root / "entrypoint.json"
    probe = root / "entrypoint.py"
    probe.write_text(
        "import json,os,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'home':os.environ['HOME'],"
        "'path':os.environ['PATH'],'profile':os.environ.get('TERMDECK_FIXTURE_PROFILE'),"
        "'scope':os.environ.get('TERMDECK_AUTH_SCOPE_ID'),'tty':os.isatty(0)}))\n"
        "time.sleep(0.3)\nsys.exit(73)\n"
    )
    extra = {}
    if private_scope:
        private = root / "auth-configured"
        extra = dict(codex_home=str(private), auth_scope_id="auth-configured")
    configured = record(root, tag="web-agent",
                        command="exec " + shlex.join([sys.executable, str(probe), str(output)]), **extra)
    tmux = backend()
    spec = tmux._launch_spec(configured)
    # Exercise the production command construction with an inert entrypoint.
    # Real plan/permit/account admission is covered in the agent-launch suite.
    tmux._run(["new-session", "-d", "-s", configured.tmux_name,
               *tmux._environment_args(spec.environment), "-c", str(spec.directory), spec.command])
    tmux._run(["set-option", "-t", configured.tmux_name, "remain-on-exit", "on"])
    observed = wait_json(output)
    assert observed["home"] == str(home)
    assert observed["profile"] == "loaded"
    assert observed["path"].startswith("/profile/npm/bin:")
    assert observed["tty"] is True
    if private_scope:
        assert observed["scope"] == "auth-configured"
    lifecycle = wait_dead(tmux, configured)
    assert lifecycle.exit_status == 73
    captured = tmux._run(["capture-pane", "-p", "-t", configured.tmux_name]).stdout
    assert "interactive shell ready" not in captured


def test_rejected_native_plan_never_reaches_profile_or_tmux_publication(tmp_path):
    from termdeck.client_files import StagingError

    tmux = backend()
    configured = record(tmp_path, tag="web-agent", command="exec /usr/bin/false")
    with mock.patch("termdeck.agent_launch.validate_native_launch",
                    side_effect=StagingError("legacy configured plan requires new authority")), \
         mock.patch.object(tmux, "session_exists", return_value=False), \
         mock.patch.object(tmux, "_with_explicit_private_environment") as profile, \
         mock.patch.object(tmux, "_run") as publish:
        with pytest.raises(TmuxError, match="requires new authority"):
            tmux.create_or_resume(configured)
    profile.assert_not_called()
    publish.assert_not_called()
