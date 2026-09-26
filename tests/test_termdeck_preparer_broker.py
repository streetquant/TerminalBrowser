from __future__ import annotations

import copy
import errno
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
    consumer = tmp_path / "consumer" / "termdeck-lan"
    libexec = tmp_path / "usr" / "libexec"
    libexec.mkdir(parents=True)
    installed_broker = libexec / "termdeck-preparer-broker"
    installed_broker.write_bytes(BROKER_PATH.read_bytes())
    installed_broker.chmod(0o755)
    executable = libexec / "termdeck-web-prepare-runtime"
    executable.write_text("#!/usr/bin/bash\nexit 0\n", encoding="ascii")
    executable.chmod(0o755)
    publication = libexec / "termdeck-preparer-publication"
    publication.write_text("#!/usr/bin/bash\nexit 0\n", encoding="ascii")
    publication.chmod(0o755)
    return broker.BrokerConfig(
        broker_path=installed_broker,
        preparer_path=executable,
        publication_path=publication,
        runtime_root=runtime,
        consumer_root=consumer,
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
    assert broker.DEFAULT_RUNTIME_ROOT == Path("/var/lib/termdeck-preparer/runtime")
    assert broker.DEFAULT_CONSUMER_ROOT == Path(
        "/warp/projects/softwares/termdeck/.termdeck"
    )
    assert broker.DEFAULT_PUBLICATION == Path(
        "/usr/libexec/termdeck-preparer-publication"
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
            publication_path=config.publication_path,
            runtime_root=config.runtime_root,
            consumer_root=config.consumer_root,
            socket_path=config.socket_path,
            root_uid=config.root_uid,
            root_gid=config.root_gid,
        )
    with pytest.raises(broker.BrokerError, match="must be disjoint"):
        broker.BrokerConfig(
            broker_path=config.broker_path,
            preparer_path=config.preparer_path,
            publication_path=config.publication_path,
            runtime_root=config.consumer_root,
            consumer_root=config.consumer_root,
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
    principals = broker.Principals(
        preparer_uid=config.root_uid + 100,
        preparer_gid=config.root_gid + 100,
        caller_uid=config.root_uid + 200,
        caller_gid=config.root_gid + 200,
    )
    monkeypatch.setattr(broker, "_validate_socket_path", lambda *_args, **_kwargs: None)
    publication_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    preparer_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    restore_result = {
        "ok": True,
        "action": "restored-tls-generation",
        "generation": "b" * 32,
        "receipt": "absent",
        "restored": {"fixture": "producer-restored"},
        "retained": {"fixture": "producer-retained"},
    }
    restore_output = broker._canonical_json(restore_result)
    outcome: list[int] = []

    def fake_run(command, **kwargs):
        executable = Path(os.readlink(command[0]))
        if executable.samefile(config.publication_path):
            operation = command[1]
            publication_calls.append((tuple(command), kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=broker._canonical_json(
                    {
                        "ok": True,
                        "operation": operation,
                        "schema": broker.PUBLICATION_SCHEMA,
                        **({"result": restore_result} if operation == "restore" else {}),
                    }
                ),
                stderr=b"",
            )
        assert executable.samefile(config.preparer_path)
        preparer_calls.append((tuple(command), kwargs))
        return SimpleNamespace(returncode=0, stdout=restore_output, stderr=b"")

    worker = threading.Thread(
        target=lambda: outcome.append(
            broker.serve_once(
                listener,
                config=config,
                resolve_principals=lambda _config: principals,
                peer_credentials=lambda _connection: broker.PeerCredentials(
                    os.getpid(), principals.caller_uid, principals.caller_gid
                ),
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
        "result": {
            "producer": restore_result,
            "publication": restore_result,
        },
        "returncode": 0,
        "schema": broker.RESPONSE_SCHEMA,
    }
    assert [command[1] for command, _kwargs in publication_calls] == ["seed", "restore"]
    assert publication_calls[0][1]["stdin"] == subprocess.DEVNULL
    assert publication_calls[1][1]["input"] == restore_output
    assert len(preparer_calls) == 1
    command, kwargs = preparer_calls[0]
    assert command[1:] == ("--restore-tls-generation", "b" * 32)
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] == "/"
    assert kwargs["user"] == principals.preparer_uid
    assert kwargs["group"] == principals.preparer_gid
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


def _verification_pair():
    def claim(path, mode, inode):
        return {
            "path": path, "mode": mode, "sha256": "c" * 64,
            "identity": [1, inode, mode, 1, 20, 30, 40],
        }

    outputs = {
        name: claim(name, 0o600, index)
        for index, name in enumerate(("current-lan-ip", "nginx/allowed-hosts.map"), 10)
    }
    manifest = {
        name: {key: value for key, value in claim(name, mode, index).items() if key != "path"}
        for index, (name, mode) in enumerate(broker._TLS_MODES.items(), 20)
    }
    receipt = {
        "schema": "termdeck.lan-runtime-build.v2", "ok": True,
        "producer_attestation": {"unchanged-signed-payload": True},
        "runtime_publication": {"generation": "a" * 32, "outputs": outputs},
        "tls_publication": {
            "generation": "b" * 32, "prior": None,
            "current": {"path": "tls", "identity": [1, 200, 0o700, 101], "manifest": manifest},
        },
    }
    content = broker._canonical_json(receipt) + b"\n"
    observed = {
        "receipt": receipt, "sha256": hashlib.sha256(content).hexdigest(),
        "identity": [1, 300, 0o644, 1, len(content), 30, 40],
    }
    publication = {
        "schema": broker.PUBLICATION_SCHEMA, "operation": "verify", "ok": True,
        "generation": "a" * 32, "producer_receipt_sha256": observed["sha256"],
        "receipt": {"path": "build-receipt.json", "mode": 0o644,
                    "sha256": observed["sha256"], "identity": [1, 400, 0o644, 1, len(content), 50, 60]},
        "outputs": copy.deepcopy(outputs),
        "tls": {"path": "tls", "identity": [1, 500, 0o700, 202], "manifest": {
            name: dict(copy.deepcopy(value), path=name) for name, value in manifest.items()
        }},
    }
    for value in [*publication["outputs"].values(), *publication["tls"]["manifest"].values()]:
        value["identity"][1] += 1000
    return observed, publication


def test_verification_binds_consumer_inodes_without_changing_signed_producer_receipt():
    observed, publication = _verification_pair()
    before = broker._canonical_json(observed)
    result = broker._bind_consumer_receipt(
        observed, publication, principals=broker.Principals(101, 101, 202, 202),
    )
    assert set(result) == {"receipt", "identity", "sha256", "publication"}
    assert result["receipt"] is observed["receipt"]
    assert result["identity"] == publication["receipt"]["identity"]
    assert result["identity"] != observed["identity"]
    assert result["publication"]["outputs"] != observed["receipt"]["runtime_publication"]["outputs"]
    assert broker._canonical_json(observed) == before


@pytest.mark.parametrize("path,replacement", [
    (("generation",), "b" * 32),
    (("producer_receipt_sha256",), "d" * 64),
    (("receipt", "path"), "tls/server.key"),
    (("receipt", "sha256"), "d" * 64),
    (("receipt", "identity", 4), 0),
    (("receipt", "identity", 1), True),
    (("outputs", "current-lan-ip", "path"), "../current-lan-ip"),
    (("outputs", "current-lan-ip", "sha256"), "d" * 64),
    (("outputs", "current-lan-ip", "identity", 4), 21),
    (("outputs", "nginx/allowed-hosts.map", "mode"), 0o644),
    (("tls", "identity", 3), 101),
    (("tls", "identity", 2), 0o755),
    (("tls", "manifest", "server.key", "sha256"), "d" * 64),
    (("tls", "manifest", "server.ext", "identity", 4), 21),
    (("tls", "manifest", "ca.crt", "path"), "/foreign/ca.crt"),
])
def test_verification_refuses_mismatched_consumer_publication(path, replacement):
    observed, publication = _verification_pair()
    current = publication
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = replacement
    with pytest.raises(broker.BrokerError):
        broker._bind_consumer_receipt(
            observed, publication, principals=broker.Principals(101, 101, 202, 202),
        )


def _restore_pair():
    observed, publication = _verification_pair()
    restored_outputs = {
        name: {key: value[key] for key in ("identity", "sha256")}
        for name, value in observed["receipt"]["runtime_publication"]["outputs"].items()
    }
    producer = {
        "ok": True, "action": "restored-runtime-generation", "generation": "b" * 32,
        "receipt": {"path": "build-receipt.json", "identity": observed["identity"], "sha256": observed["sha256"]},
        "restored": {"outputs": restored_outputs, "runtime_generation": "a" * 32,
                     "tls": observed["receipt"]["tls_publication"]["current"]},
        "retained": {"fixture": "unchanged authenticated producer lineage"},
    }
    consumer = copy.deepcopy(producer)
    consumer["receipt"] = publication["receipt"]
    consumer["restored"]["outputs"] = publication["outputs"]
    return producer, {"schema": broker.PUBLICATION_SCHEMA, "operation": "restore", "ok": True, "result": consumer}


def test_restore_keeps_producer_lineage_and_consumer_claims_separate():
    producer, publication = _restore_pair()
    raw = broker._canonical_json(producer)
    result = broker._bind_consumer_restore(
        raw, publication, request=broker.PrepareRequest("generation", "b" * 32),
    )
    assert broker._canonical_json(result["producer"]) == raw
    assert result["publication"] == publication["result"]
    assert result["producer"]["receipt"]["identity"] != result["publication"]["receipt"]["identity"]


@pytest.mark.parametrize("path,replacement", [
    (("generation",), "c" * 32),
    (("restored", "runtime_generation"), "c" * 32),
    (("restored", "tls", "identity", 1), 99999),
    (("restored", "outputs", "current-lan-ip", "sha256"), "d" * 64),
    (("receipt", "identity", 4), 0),
    (("receipt", "path"), "/foreign/build-receipt.json"),
    (("retained", "fixture"), "changed lineage"),
    (("consumer_cleanup_warnings",), ["x" * 257]),
    (("consumer_cleanup_warnings",), ["injected\nmessage"]),
    (("consumer_cleanup_warnings",), None),
])
def test_restore_refuses_changed_lineage_or_mismatched_consumer_content(path, replacement):
    producer, publication = _restore_pair()
    current = publication["result"]
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = replacement
    with pytest.raises(broker.BrokerError):
        broker._bind_consumer_restore(
            broker._canonical_json(producer), publication,
            request=broker.PrepareRequest("generation", "b" * 32),
        )


@pytest.mark.parametrize("failure,phase,category", [
    ("invalid-request", "request", "contract"),
    ("seed", "publication-seed", "child-refused"),
    ("execute", "preparer", "execution-permission-denied"),
    ("timeout", "preparer", "timeout"),
    ("preparer", "preparer", "child-failed"),
    ("publish", "publication-publish", "child-refused"),
])
def test_refusal_diagnostics_use_only_fixed_phase_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    failure: str, phase: str, category: str,
):
    socket_path = _socket_path(tmp_path)
    config = _config(tmp_path, socket_path)
    principals = broker.Principals(config.root_uid + 100, config.root_gid + 100,
                                   config.root_uid + 200, config.root_gid + 200)
    monkeypatch.setattr(broker, "_validate_socket_path", lambda *_args: None)
    marker = "fixture-token-key-data-private\nforged-journal-message"

    def run(command, **options):
        assert options["stderr"] == subprocess.DEVNULL
        if Path(os.readlink(command[0])).samefile(config.publication_path):
            operation = command[1]
            return SimpleNamespace(
                returncode=2 if failure == operation else 0,
                stdout=broker._canonical_json({"schema": broker.PUBLICATION_SCHEMA, "operation": operation, "ok": True}),
                stderr=marker.encode(),
            )
        assert options["stdout"] == subprocess.DEVNULL
        if failure == "execute":
            raise OSError(errno.EPERM, marker)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(marker, 1, output=marker, stderr=marker)
        return SimpleNamespace(returncode=2 if failure == "preparer" else 0, stdout=marker, stderr=marker)

    outcomes = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        listener.listen(1)
        worker = threading.Thread(target=lambda: outcomes.append(broker.serve_once(
            listener, config=config, resolve_principals=lambda _config: principals,
            peer_credentials=lambda _connection: broker.PeerCredentials(123, principals.caller_uid, principals.caller_gid),
            run_process=run,
        )))
        worker.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(4)
                client.connect(str(socket_path))
                request = _request()
                if failure == "invalid-request":
                    request["secret"] = marker
                client.sendall(broker.frame(request))
                response = _receive_frame(client)
            worker.join(timeout=4)
        finally:
            socket_path.unlink()
    assert not worker.is_alive()
    assert outcomes == [1]
    assert response["ok"] is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"TermDeck preparer broker refused: phase={phase} category={category}\n"
    assert marker not in json.dumps(response) + captured.err


def test_response_frame_limit_does_not_expand_closed_request_limit(tmp_path: Path):
    assert broker.MAX_FRAME_BYTES == 128 * 1024
    assert broker.MAX_REQUEST_BYTES == 4096
    payload = {"receipt": "x" * (17 * 1024)}
    encoded = broker.frame(payload)
    assert struct.unpack("!I", encoded[:4])[0] == len(encoded) - 4
    assert json.loads(encoded[4:]) == payload
    with pytest.raises(broker.BrokerError, match="frame size"):
        broker.frame({"receipt": "x" * broker.MAX_FRAME_BYTES})
    config = _config(tmp_path)
    left, right = socket.socketpair()
    try:
        right.sendall(struct.pack("!I", broker.MAX_REQUEST_BYTES + 1))
        with pytest.raises(broker.BrokerError, match="frame size"):
            broker.receive_request(left, config=config)
    finally:
        left.close()
        right.close()


def test_diagnostic_codes_reject_arbitrary_exception_or_input_text(capsys):
    marker = "fixture-secret-token\nforged journal text"
    error = broker.BrokerError(marker, category=marker)
    broker._diagnose(marker, error.category)
    assert capsys.readouterr().err == (
        "TermDeck preparer broker refused: phase=startup category=contract\n"
    )
