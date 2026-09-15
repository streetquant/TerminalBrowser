from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROKER_PATH = ROOT / "deploy" / "termdeck-preparer-broker"


def _load_broker():
    name = "termdeck_preparer_broker_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(BROKER_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


broker = _load_broker()


def _config(tmp_path: Path, socket_path: Path | None = None):
    runtime = tmp_path / "runtime" / "termdeck-lan"
    libexec = tmp_path / "usr" / "libexec"
    libexec.mkdir(parents=True)
    installed_broker = libexec / "termdeck-preparer-broker"
    installed_broker.write_bytes(BROKER_PATH.read_bytes())
    installed_broker.chmod(0o755)
    executable = libexec / "termdeck-web-prepare-runtime"
    executable.write_text("#!/usr/bin/bash\nexit 0\n", encoding="ascii")
    executable.chmod(0o755)
    return broker.BrokerConfig(
        broker_path=installed_broker,
        preparer_path=executable,
        runtime_root=runtime,
        socket_path=socket_path or tmp_path / "request.sock",
        root_uid=os.getuid(),
        root_gid=os.getgid(),
        request_timeout=2.0,
        preparer_timeout=3.0,
    )


def _principals(config):
    return broker.Principals(
        preparer_uid=config.root_uid + 100,
        preparer_gid=config.root_gid + 100,
        caller_uid=os.getuid(),
        caller_gid=os.getgid(),
    )


def _request(mode: str = "none", generation: str | None = None) -> dict[str, object]:
    return {
        "operation": "prepare",
        "restore": {"generation": generation, "mode": mode},
        "schema": broker.REQUEST_SCHEMA,
    }


def _receive_frame(connection: socket.socket) -> dict[str, object]:
    header = connection.recv(4)
    assert len(header) == 4
    remaining = struct.unpack("!I", header)[0]
    content = b""
    while len(content) < remaining:
        content += connection.recv(remaining - len(content))
    return json.loads(content)


def _socket_path(tmp_path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(tmp_path)).hexdigest()[:16]
    return Path(os.environ.get("TMPDIR", "/var/tmp")) / f"tdpb-{digest}.sock"


def test_request_contract_has_only_allowlisted_restore_modes():
    assert broker.DEFAULT_PREPARER == Path(
        "/usr/libexec/termdeck-web-prepare-runtime"
    )
    assert broker.DEFAULT_RUNTIME_ROOT == Path(
        "/warp/runtime/termdeck/termdeck-lan"
    )
    assert broker.validate_request(_request()) == broker.PrepareRequest("none", None)
    assert broker.validate_request(_request("unreceipted")) == broker.PrepareRequest(
        "unreceipted", None
    )
    generation = "a" * 32
    assert broker.validate_request(
        _request("generation", generation)
    ).child_arguments == ("--restore-tls-generation", generation)

    rejected = (
        {**_request(), "path": "/tmp/foreign"},
        {**_request(), "fd": 7},
        {**_request(), "executable": "/tmp/foreign"},
        {**_request(), "operation": "shell"},
        _request("generation", "A" * 32),
        _request("generation", None),
        _request("none", "a" * 32),
        _request("foreign"),
    )
    for candidate in rejected:
        with pytest.raises(broker.BrokerError):
            broker.validate_request(candidate)

    noncanonical = json.dumps(_request(), indent=2).encode("ascii")
    with pytest.raises(broker.BrokerError, match="canonical"):
        broker._strict_json(noncanonical)


def test_config_keeps_executable_outside_mutable_runtime(tmp_path: Path):
    config = _config(tmp_path)
    with pytest.raises(broker.BrokerError, match="fixed libexec layout"):
        broker.BrokerConfig(
            broker_path=config.broker_path,
            preparer_path=config.runtime_root / "bin" / "termdeck-web-prepare",
            runtime_root=config.runtime_root,
            socket_path=config.socket_path,
            root_uid=config.root_uid,
            root_gid=config.root_gid,
        )


def test_socket_activation_requires_one_exact_named_unix_listener(tmp_path: Path):
    socket_path = _socket_path(tmp_path)
    config = _config(tmp_path, socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    duplicate = os.dup(listener.fileno())
    environment = {
        "LISTEN_PID": "4242",
        "LISTEN_FDS": "1",
        "LISTEN_FDNAMES": broker.SOCKET_FD_NAME,
    }
    activated = broker.activation_listener(
        config=config,
        environ=environment,
        process_id=4242,
        descriptor=duplicate,
    )
    try:
        assert activated.getsockname() == str(socket_path)
        assert activated.get_inheritable() is False
    finally:
        activated.close()
        listener.close()
        socket_path.unlink()

    for key, value in (
        ("LISTEN_PID", "4243"),
        ("LISTEN_FDS", "2"),
        ("LISTEN_FDNAMES", "foreign"),
    ):
        invalid = dict(environment)
        invalid[key] = value
        with pytest.raises(broker.BrokerError, match="one exact"):
            broker.activation_listener(
                config=config,
                environ=invalid,
                process_id=4242,
                descriptor=3,
            )


def test_socket_path_rejects_a_gid_that_is_not_the_resolved_primary_gid(
    tmp_path: Path,
) -> None:
    socket_path = _socket_path(tmp_path)
    config = _config(tmp_path, socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    principals = _principals(config)
    try:
        with pytest.raises(broker.BrokerError, match="ownership or mode"):
            broker._validate_socket_path(
                config,
                broker.Principals(
                    principals.preparer_uid,
                    principals.preparer_gid,
                    principals.caller_uid,
                    principals.caller_gid + 1,
                ),
            )
    finally:
        listener.close()
        socket_path.unlink()


def test_socket_broker_authenticates_peer_and_runs_fixed_preparer_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    socket_path = _socket_path(tmp_path)
    config = _config(tmp_path, socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(2)
    monkeypatch.setattr(os, "getresuid", lambda: (config.root_uid,) * 3)
    monkeypatch.setattr(os, "getresgid", lambda: (config.root_gid,) * 3)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    outcome: list[int] = []

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        assert Path(os.readlink(command[0])).samefile(config.preparer_path)
        return SimpleNamespace(returncode=0)

    worker = threading.Thread(
        target=lambda: outcome.append(
            broker.serve_once(
                listener,
                config=config,
                resolve_principals=_principals,
                run_process=fake_run,
            )
        )
    )
    worker.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(broker.frame(_request("generation", "b" * 32)))
        response = _receive_frame(client)
    worker.join(timeout=5)
    listener.close()
    socket_path.unlink()

    assert worker.is_alive() is False
    assert outcome == [0]
    assert response == {
        "error": None,
        "ok": True,
        "returncode": 0,
        "schema": broker.RESPONSE_SCHEMA,
    }
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:] == ("--restore-tls-generation", "b" * 32)
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["cwd"] == "/"
    assert kwargs["user"] == config.root_uid + 100
    assert kwargs["group"] == config.root_gid + 100
    assert kwargs["extra_groups"] == ()
    assert kwargs["pass_fds"] == (int(command[0].rsplit("/", 1)[1]),)
    assert kwargs["timeout"] == config.preparer_timeout
    assert kwargs["umask"] == 0o077
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "TERMDECK_PREPARER_BROKER": "1",
        "TERMDECK_RUNTIME_ROOT": str(config.runtime_root),
    }


def test_socket_broker_rejects_foreign_peer_and_scm_rights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(tmp_path)
    monkeypatch.setattr(os, "getresuid", lambda: (config.root_uid,) * 3)
    monkeypatch.setattr(os, "getresgid", lambda: (config.root_gid,) * 3)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    read_fd, write_fd = os.pipe()
    try:
        right.sendmsg(
            [broker.frame(_request())],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", read_fd))],
        )
        with pytest.raises(broker.BrokerError, match="may not carry descriptors"):
            broker.receive_request(left, config=config)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        left.close()
        right.close()

    peer = broker.PeerCredentials(123, _principals(config).caller_uid + 1, 456)
    with pytest.raises(broker.BrokerError, match="not max"):
        broker._assert_peer(peer, principals=_principals(config))
