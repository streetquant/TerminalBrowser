from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from termdeck import service_wrappers as wrappers


ROOT = Path(__file__).resolve().parents[1]
UNIT = "termdeck-web-lan.service"
NGINX = Path("/usr/bin/nginx")
COMMAND = (str(NGINX), "-p", "/fixture/nginx/", "-c", "/fixture/nginx/nginx.conf", "-g", "daemon off;")


def _title(command: tuple[str, ...] = COMMAND) -> str:
    return "nginx: master process " + " ".join(command)


@pytest.mark.parametrize("padding", ((), ("",), ("", "", "")))
def test_exact_nginx_title_accepts_only_nul_padding(monkeypatch, padding):
    identity = {"pid": 4242, "start_ticks": 1234, "exe": str(NGINX.resolve()), "argv": list(COMMAND)}
    current = dict(identity, argv=[_title(), *padding])
    monkeypatch.setattr(wrappers, "process_identity", lambda _pid: current)
    assert wrappers.verify_process_identity(identity)


@pytest.mark.parametrize(
    ("current_change", "recorded_change"),
    (
        pytest.param({"argv": ["nginx: worker process"]}, {}, id="worker-title"),
        pytest.param({"argv": ["nginx: unrelated process"]}, {}, id="unrelated-title"),
        pytest.param({"argv": [_title()[:-1]]}, {}, id="truncated-title"),
        pytest.param({"argv": [_title() + " -s reload"]}, {}, id="extra-title-argument"),
        pytest.param({"argv": [_title(), "foreign"]}, {}, id="nonempty-padding"),
        pytest.param({"argv": ["", _title()]}, {}, id="leading-empty-argument"),
        pytest.param({"argv": [_title().replace("daemon off;", "daemon on;")]}, {}, id="changed-argument"),
        pytest.param({"argv": [_title().replace(" -p ", " -c ")]}, {}, id="changed-option"),
        pytest.param({"argv": [_title().replace("/fixture/nginx/nginx.conf", "/foreign.conf")]}, {}, id="foreign-config"),
        pytest.param({"exe": str(Path(sys.executable).resolve())}, {}, id="unrelated-executable"),
        pytest.param({}, {"exe": str(Path(sys.executable).resolve())}, id="foreign-recorded-executable"),
        pytest.param({"start_ticks": 1235}, {}, id="reused-pid"),
        pytest.param({"pid": 4243}, {}, id="foreign-pid"),
        pytest.param(
            {"argv": ["nginx: master process /usr/bin/python3 script.py"], "exe": str(Path(sys.executable).resolve())},
            {"argv": ["/usr/bin/python3", "script.py"], "exe": str(Path(sys.executable).resolve())},
            id="title-on-another-command",
        ),
        pytest.param(
            {"argv": [_title()], "exe": "/fixture/nginx-copy"},
            {"exe": "/fixture/nginx-copy"},
            id="copied-executable",
        ),
    ),
)
def test_nginx_title_does_not_relax_other_identity_fields(monkeypatch, current_change, recorded_change):
    recorded = {"pid": 4242, "start_ticks": 1234, "exe": str(NGINX.resolve()), "argv": list(COMMAND)}
    current = dict(recorded, argv=[_title()])
    current.update(current_change)
    recorded.update(recorded_change)
    monkeypatch.setattr(wrappers, "process_identity", lambda _pid: current)
    assert wrappers.verify_process_identity(recorded) is False


def test_proc_argv_preserves_raw_empty_arguments(monkeypatch):
    raw = _title().encode() + b"\0\0\0"
    monkeypatch.setattr(Path, "read_bytes", lambda _path: raw)
    assert wrappers.proc_argv(4242) == (_title(), "", "")


def _nginx_layout(tmp_path: Path) -> dict[str, Path]:
    if not NGINX.is_file():
        pytest.skip("the disposable nginx check requires /usr/bin/nginx")
    version = subprocess.run([str(NGINX), "-V"], capture_output=True, text=True, check=True)
    if "--error-log-path=stderr" not in version.stderr:
        pytest.skip("the disposable canonical command requires nginx's initial log to use stderr")
    paths = {"source_root": ROOT}
    for key in ("control_root", "runtime_root", "state_root", "managed_root", "managed_bin", "unit_dir"):
        paths[key] = tmp_path / key
        paths[key].mkdir(mode=0o700)
    layout = wrappers.resolve_layout(**paths)
    runtime = wrappers._runtime_paths(layout)
    runtime["nginx_root"].mkdir(parents=True, mode=0o700)
    runtime["nginx_conf"].write_text(
        "worker_processes 1;\nerror_log stderr notice;\n"
        + "pid " + json.dumps(str(runtime["nginx_pid"])) + ";\n"
        + "events { worker_connections 16; }\n",
        encoding="ascii",
    )
    runtime["nginx_conf"].chmod(0o600)
    return paths


@contextmanager
def _disposable_process(command):
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        start_new_session=True,
    )
    descriptor = os.pidfd_open(process.pid)
    try:
        yield process
    finally:
        # This pidfd belongs only to the child created above, including across
        # the wrapper's exec. Never discover or signal an existing service.
        for chosen in (signal.SIGQUIT, signal.SIGTERM, signal.SIGKILL):
            if process.poll() is not None:
                break
            signal.pidfd_send_signal(descriptor, chosen, None, 0)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                continue
        os.close(descriptor)
        assert process.poll() is not None, "disposable nginx failed to exit"
        process.stderr.close()


def _wait_for_master(process, pid_file: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("disposable nginx exited before startup: " + process.stderr.read().decode())
        identity = wrappers.process_identity(process.pid)
        if identity["argv"] and identity["argv"][0].startswith("nginx: master process ") and pid_file.exists():
            assert int(pid_file.read_text()) == process.pid
            return identity
        time.sleep(0.01)
    pytest.fail("disposable nginx did not publish its master title")


def _assert_no_listeners(pid: int) -> None:
    inodes = set()
    for descriptor in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    for protocol in ("tcp", "tcp6", "udp", "udp6"):
        for line in Path(f"/proc/{pid}/net/{protocol}").read_text().splitlines()[1:]:
            fields = line.split()
            assert fields[9] not in inodes, "disposable nginx opened an Internet socket"
    for line in Path(f"/proc/{pid}/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[6] in inodes:
            assert int(fields[3], 16) & 0x10000 == 0, "disposable nginx opened a Unix listener"


def test_real_nginx_master_matches_exact_launch_identity(tmp_path: Path) -> None:
    paths = _nginx_layout(tmp_path)
    layout = wrappers.resolve_layout(**paths)
    runtime = wrappers._runtime_paths(layout)
    command = wrappers.build_start_command(UNIT, **paths)
    with _disposable_process(command) as process:
        observed = _wait_for_master(process, runtime["nginx_pid"])
        _assert_no_listeners(process.pid)
        assert observed["argv"][0] == _title(command)
        expected = dict(observed, argv=list(command))
        assert wrappers.verify_process_identity(expected)
        assert wrappers.process_identity(process.pid) == observed
        assert wrappers.verify_process_identity(observed)
        assert not wrappers.verify_process_identity(dict(expected, start_ticks=expected["start_ticks"] + 1))
        assert not wrappers.verify_process_identity(dict(expected, exe=str(Path(sys.executable).resolve())))
        assert not wrappers.verify_process_identity(dict(expected, argv=[*command[:-1], "daemon on;"]))


def test_disposable_nginx_wrapper_start_and_guarded_stop(tmp_path: Path) -> None:
    paths = _nginx_layout(tmp_path)
    layout = wrappers.resolve_layout(**paths)
    runtime = wrappers._runtime_paths(layout)
    wrappers.publish_wrappers("nginx-disposable", **paths)
    script = """
import json, sys
from pathlib import Path
from termdeck import service_wrappers as wrappers
paths = json.loads(sys.argv[1])
expected_runtime = Path(paths['runtime_root']) / 'termdeck-lan'
def preparation(*, runtime_root):
    assert runtime_root == expected_runtime
# Only the privileged preparation dependency is replaced: all inputs below
# this fixture were prepared locally and no production broker is contacted.
wrappers._request_lan_preparation = preparation
raise SystemExit(wrappers.run_wrapper('termdeck-web-lan.service', 'start', release_id='nginx-disposable', **paths))
"""
    command = [sys.executable, "-B", "-c", script, json.dumps({key: str(path) for key, path in paths.items()})]
    with _disposable_process(command) as process:
        observed = _wait_for_master(process, runtime["nginx_pid"])
        _assert_no_listeners(process.pid)
        config_path = layout.config_path(wrappers.UNIT_SPEC_BY_NAME[UNIT])
        recorded = json.loads(config_path.read_bytes())
        assert recorded["pid"] == process.pid
        assert recorded["start_ticks"] == observed["start_ticks"]
        assert tuple(recorded["argv"]) == wrappers.build_start_command(UNIT, **paths)
        assert wrappers.verify_process_identity(recorded)
        assert wrappers.run_wrapper(UNIT, "stop", release_id="nginx-disposable", stop_timeout=3, **paths) == 0
        assert process.wait(timeout=3) == 0
        stopped = json.loads(config_path.read_bytes())
        assert stopped["state"] == "stopped"
        assert stopped["pid"] is None and stopped["start_ticks"] is None and stopped["exe"] is None
        assert stopped["argv"] == [] and stopped["command_sha256"] is None
        assert wrappers.run_wrapper(UNIT, "stop", release_id="nginx-disposable", **paths) == 0


@pytest.mark.parametrize("worker_timeout", (True, False), ids=("nginx-drain", "pidfd-fallback"))
def test_disposable_nginx_stop_with_open_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_timeout: bool
) -> None:
    paths = _nginx_layout(tmp_path)
    layout = wrappers.resolve_layout(**paths)
    runtime = wrappers._runtime_paths(layout)
    with socket.socket() as upstream:
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        upstream.settimeout(8)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            listen_port = reservation.getsockname()[1]
        config = (
            "worker_processes 1;\nerror_log stderr notice;\n"
            + "pid " + json.dumps(str(runtime["nginx_pid"])) + ";\n"
            + ("worker_shutdown_timeout 1s;\n" if worker_timeout else "")
            + "events { worker_connections 16; }\n"
            + "http { access_log off; client_body_temp_path "
            + json.dumps(str(runtime["nginx_root"])) + ";\n"
            + "server { listen 127.0.0.1:" + str(listen_port) + ";\n"
            + "location /hold { proxy_pass http://127.0.0.1:" + str(upstream.getsockname()[1]) + ";\n"
            + 'proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; } } }\n'
        )
        runtime["nginx_conf"].write_text(config, encoding="ascii")
        runtime["nginx_conf"].chmod(0o600)
        wrappers.publish_wrappers("nginx-websocket", **paths)
        script = """
import json, sys
from pathlib import Path
from termdeck import service_wrappers as wrappers
paths = json.loads(sys.argv[1])
def preparation(*, runtime_root):
    assert runtime_root == Path(paths['runtime_root']) / 'termdeck-lan'
wrappers._request_lan_preparation = preparation
raise SystemExit(wrappers.run_wrapper('termdeck-web-lan.service', 'start', release_id='nginx-websocket', **paths))
"""
        command = [sys.executable, "-B", "-c", script, json.dumps({key: str(path) for key, path in paths.items()})]
        with _disposable_process(command) as process:
            _wait_for_master(process, runtime["nginx_pid"])
            with socket.create_connection(("127.0.0.1", listen_port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(
                    b"GET /hold HTTP/1.1\r\nHost: localhost\r\n"
                    b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
                )
                backend, _ = upstream.accept()
                with backend:
                    backend.settimeout(5)
                    assert b"GET /hold" in backend.recv(4096)
                    backend.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
                    )
                    assert b"101 Switching Protocols" in client.recv(4096)
                    if not worker_timeout:
                        monkeypatch.setattr(wrappers, "LAN_GRACEFUL_STOP_TIMEOUT", 0.3)
                    started = time.monotonic()
                    assert wrappers.run_wrapper(
                        UNIT, "stop", release_id="nginx-websocket", stop_timeout=8, **paths
                    ) == 0
                    elapsed = time.monotonic() - started
                    assert elapsed < 5, f"open WebSocket blocked stop for {elapsed:.2f}s"
                    assert process.wait(timeout=2) == 0
                    assert client.recv(1) == b""
                    stopped = json.loads(layout.config_path(wrappers.UNIT_SPEC_BY_NAME[UNIT]).read_bytes())
                    assert stopped["state"] == "stopped" and stopped["pid"] is None
                    assert wrappers.run_wrapper(UNIT, "stop", release_id="nginx-websocket", **paths) == 0
