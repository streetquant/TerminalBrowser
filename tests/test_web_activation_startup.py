"""Type=simple startup with real disposable wrapper, exec and nginx identities.

Only systemd's transport and the preparer delay are fixtures. No host service
or broker is contacted; recorded configs, /proc checks and nginx are real.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from termdeck import service_wrappers as wrappers
from termdeck import web_activation as activation


@pytest.fixture
def startup(tmp_path, monkeypatch):
    paths = activation.ActivationPaths(
        target=tmp_path / "source", evidence=tmp_path / "state/recovery/activation-v2",
        runtime=tmp_path / "runtime/termdeck-lan", database=tmp_path / "state/sessions.sqlite3",
        managed_root=tmp_path / "managed", managed_bin=tmp_path / "bin", unit_dir=tmp_path / "units",
    )
    for path in (paths.target, paths.runtime / "bin", paths.runtime / "nginx", paths.database.parent,
                 paths.managed_root, paths.managed_bin, paths.unit_dir):
        path.mkdir(parents=True, exist_ok=True)
    release_id = "startup-fixture-A"
    activation._atomic(paths.managed_root / "state.json", wrappers._canonical_json({
        "current_release_id": release_id, "generation": 1,
    }))
    layout = wrappers.resolve_layout(**activation._wrapper_kwargs(paths))
    backend = paths.runtime / "bin/termdeck-web-server"
    backend.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(120)\n")
    backend.chmod(0o755)
    (paths.runtime / "nginx/nginx.conf").write_text(
        "pid " + str(paths.runtime / "nginx/nginx.pid") + ";\n"
        "error_log stderr;\nevents {}\nhttp {}\n"
    )
    gate = tmp_path / "preparation-complete"
    state = SimpleNamespace(
        paths=paths, layout=layout, release_id=release_id, gate=gate, children={},
        invocations={}, timers=[], info_changes={}, commands={}, starts=[],
        timeouts=[], before_show=None, legacy=False, delay=0.0, block=False,
    )
    kwargs = activation._wrapper_kwargs(paths)
    for unit in activation.SERVICES:
        spec = wrappers.UNIT_SPEC_BY_NAME[unit]
        command = wrappers.build_start_command(spec, **kwargs)
        state.commands[unit] = command
        config = wrappers._initial_config(spec, layout, release_id)
        activation._atomic(layout.config_path(spec), wrappers._canonical_json(config))
        wrapper = layout.wrapper_path(spec, "start")
        wrapper.write_text(
            "#!/usr/bin/python3\nimport os,sys,time\nfrom pathlib import Path\n"
            + "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
            + "from termdeck import service_wrappers as w\n"
            + "layout = w.resolve_layout(**" + repr({key: str(value) for key, value in kwargs.items()}) + ")\n"
            + ("while not Path(" + repr(str(gate)) + ").exists(): time.sleep(0.01)\n" if spec.kind == "lan" else "")
            + "command = " + repr(command) + "\n"
            + "w._record_start_identity(w.UNIT_SPEC_BY_NAME[" + repr(unit) + "], layout, " + repr(release_id) + ", command)\n"
            + "os.execv(command[0], list(command))\n"
        )
        wrapper.chmod(0o755)
        (paths.unit_dir / unit).write_text(
            "[Service]\nType=simple\nExecStart=" + shlex.join((str(wrapper),)) + "\n"
        )

    def run(argv, *, timeout, **_kwargs):
        assert argv[:2] == ["/usr/bin/systemctl", "--user"], argv
        action, unit = argv[2:4]
        assert unit in activation.SERVICES
        state.timeouts.append(timeout)
        if action == "start":
            state.starts.append(unit)
            command = state.commands[unit] if state.legacy else (str(layout.wrapper_path(wrappers.UNIT_SPEC_BY_NAME[unit], "start")),)
            state.children[unit] = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            state.invocations[unit] = uuid.uuid4().hex
            if unit == "termdeck-web-lan.service" and not state.block:
                timer = threading.Timer(state.delay, lambda: gate.write_text("prepared\n"))
                state.timers.append(timer)
                timer.start()
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert action == "show"
        if state.before_show is not None:
            state.before_show(unit)
        process = state.children[unit]
        info = {
            "MainPID": str(process.pid), "InvocationID": state.invocations[unit],
            "ActiveState": "active" if process.poll() is None else "failed", "SubState": "running",
            "LoadState": "loaded", "Type": "simple", "FragmentPath": str(paths.unit_dir / unit),
            "DropInPaths": "", "NeedDaemonReload": "no",
        }
        info.update(state.info_changes.get(unit, {}))
        return subprocess.CompletedProcess(argv, 0, "\n".join(f"{key}={value}" for key, value in info.items()), "")

    monkeypatch.setattr(activation, "_run", run)
    monkeypatch.setattr(wrappers, "_request_lan_preparation", lambda **_kwargs: pytest.fail("startup must not probe or mutate the broker"))
    try:
        yield state
    finally:
        for timer in state.timers:
            timer.cancel()
            timer.join(timeout=2)
        for process in state.children.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)


def test_start_waits_for_delayed_simple_wrapper_before_runtime_inspection(startup):
    startup.delay = 2.5  # Longer than the old client's total 0.3 second backoff.
    before = time.monotonic()
    activation._start_web(startup.paths)
    assert startup.gate.exists(), "Type=simple became active before LAN preparation completed"
    assert time.monotonic() - before >= 2.5
    assert startup.starts == list(activation.SERVICES)
    assert all(0 < timeout <= 90 for timeout in startup.timeouts)
    for unit, child in startup.children.items():
        spec = wrappers.UNIT_SPEC_BY_NAME[unit]
        config = wrappers._read_unit_config(startup.layout.config_path(spec), spec=spec, expected_release_id=startup.release_id)
        assert config["pid"] == child.pid
        assert wrappers.verify_process_identity(config)
    assert wrappers._read_nginx_pid(startup.layout) == startup.children["termdeck-web-lan.service"].pid


def test_legacy_direct_services_need_no_managed_config(startup):
    startup.legacy = True
    for unit, command in startup.commands.items():
        (startup.paths.unit_dir / unit).write_text("[Service]\nType=simple\nExecStart=" + shlex.join(command) + "\n")
        startup.layout.config_path(wrappers.UNIT_SPEC_BY_NAME[unit]).unlink()
    activation._start_web(startup.paths)
    assert wrappers._read_nginx_pid(startup.layout) == startup.children["termdeck-web-lan.service"].pid


def test_canonical_runtime_layout_uses_the_explicit_paths(startup, monkeypatch):
    # Exercise the canonical layout branch inside this disposable namespace.
    monkeypatch.setattr(wrappers, "CANONICAL_RUNTIME_ROOT", startup.paths.runtime)
    activation._start_web(startup.paths)
    assert wrappers._read_nginx_pid(startup.layout) == startup.children["termdeck-web-lan.service"].pid


def test_wrong_release_config_is_refused_before_any_start(startup):
    spec = wrappers.UNIT_SPEC_BY_NAME[activation.SERVICES[0]]
    config = wrappers._initial_config(spec, startup.layout, "foreign-release")
    activation._atomic(startup.layout.config_path(spec), wrappers._canonical_json(config))
    with pytest.raises(activation.WebActivationError, match="release id mismatch"):
        activation._start_web(startup.paths)
    assert startup.starts == []


@pytest.mark.parametrize("change", (
    {"ActiveState": "inactive"}, {"ActiveState": "failed"}, {"SubState": "exited"},
    {"MainPID": "0"}, {"InvocationID": "foreign"}, {"LoadState": "not-found"},
    {"Type": "oneshot"}, {"FragmentPath": "/foreign/unit.service"},
    {"DropInPaths": "/foreign/override.conf"}, {"NeedDaemonReload": "yes"},
))
def test_start_refuses_failed_or_foreign_unit(startup, change):
    startup.info_changes[activation.SERVICES[0]] = change
    with pytest.raises(activation.WebActivationError):
        activation._start_web(startup.paths)


def test_start_refuses_foreign_process_even_while_preparation_is_pending(startup):
    startup.block = True
    startup.info_changes[activation.SERVICES[0]] = {"MainPID": str(os.getpid())}
    with pytest.raises(activation.WebActivationError, match="unexpected process"):
        activation._start_web(startup.paths)


@pytest.mark.parametrize("kind", ("pid", "start-ticks", "invocation"))
def test_start_refuses_identity_drift(startup, monkeypatch, kind):
    startup.block = True
    seen = 0

    def change(unit):
        nonlocal seen
        if unit == activation.SERVICES[1]:
            seen += 1
            if seen == 2:
                if kind == "pid":
                    startup.info_changes[unit] = {"MainPID": str(os.getpid())}
                elif kind == "start-ticks":
                    original = wrappers.process_identity

                    def changed_identity(pid):
                        identity = original(pid)
                        if pid == startup.children[unit].pid:
                            identity["start_ticks"] += 1
                        return identity

                    monkeypatch.setattr(wrappers, "process_identity", changed_identity)
                else:
                    startup.invocations[unit] = uuid.uuid4().hex

    startup.before_show = change
    with pytest.raises(activation.WebActivationError, match="identity changed"):
        activation._start_web(startup.paths)


def test_start_refuses_foreign_nginx_pidfile(startup, monkeypatch):
    # The current process is owned by this fixture but is never an nginx PID.
    # Keep the wrapper pending until the owned child has its final identity,
    # then replace only the disposable pidfile before its observation.
    original = wrappers._read_nginx_pid

    def change(layout):
        path = startup.paths.runtime / "nginx/nginx.pid"
        path.write_text(str(os.getpid()) + "\n")
        return original(layout)

    monkeypatch.setattr(wrappers, "_read_nginx_pid", change)
    with pytest.raises(activation.WebActivationError, match="does not own its pidfile"):
        activation._start_web(startup.paths)


def test_pending_wrapper_cannot_extend_the_90_second_deadline(startup, monkeypatch):
    startup.block = True
    elapsed = 0.0
    sleeps = []

    def advance(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds
        time.sleep(0.001)

    monkeypatch.setattr(activation, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance))
    with pytest.raises(activation.WebActivationError, match="90 seconds"):
        activation._start_web(startup.paths)
    assert elapsed == 90.0
    assert max(sleeps) <= 0.25
    assert max(startup.timeouts) == 90


def test_start_does_not_rebind_to_canonical_paths(startup):
    wrong = dataclasses.replace(startup.paths, unit_dir=startup.paths.unit_dir / "foreign")
    with pytest.raises(activation.WebActivationError):
        activation._start_web(wrong)
    assert startup.starts == []


def test_ready_unit_replacement_is_refused_before_return(startup, monkeypatch):
    original = activation._web_service_start_ready

    def replaced(paths, unit, values, **kwargs):
        ready = original(paths, unit, values, **kwargs)
        if ready and unit == activation.SERVICES[-1]:
            path = paths.unit_dir / activation.SERVICES[0]
            # Same bytes in a new inode cannot replace the pinned unit.
            replacement = path.with_suffix(".foreign")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return ready

    monkeypatch.setattr(activation, "_web_service_start_ready", replaced)
    with pytest.raises(activation.WebActivationError, match="web startup unit changed"):
        activation._start_web(startup.paths)


def test_ready_service_does_not_make_broker_refusals_retryable(startup, monkeypatch):
    activation._start_web(startup.paths)
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise wrappers.WrapperError("preparer broker refused runtime preparation")

    monkeypatch.setattr(wrappers, "_request_lan_preparation", refuse)
    with pytest.raises(activation.WebActivationError, match="broker observation failed"):
        activation._broker_runtime_receipt(startup.paths)
    assert len(calls) == 1
    assert calls[0]["restore_mode"] == "verify"
