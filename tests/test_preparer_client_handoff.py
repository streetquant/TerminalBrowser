from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
import socket
import struct
import threading
from types import SimpleNamespace
from typing import Iterator

import pytest

from termdeck import service_wrappers as wrappers


VERIFY_RESULT = {"receipt": {"fixture": True}, "publication": {"operation": "verify"}}


def _response(**changes: object) -> dict[str, object]:
    return {
        "error": None,
        "ok": True,
        "returncode": 0,
        "schema": wrappers.PREPARER_BROKER_RESPONSE_SCHEMA,
        "result": VERIFY_RESULT,
        **changes,
    }


def _frame(payload: bytes) -> bytes:
    return struct.pack("!I", len(payload)) + payload


def _receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        assert block, "client closed before sending its request"
        result.extend(block)
    return bytes(result)


@contextmanager
def _one_shot_broker(
    tmp_path_factory: pytest.TempPathFactory, actions: list[str | bytes],
) -> Iterator[SimpleNamespace]:
    # A short private pathname also fits the authoritative runner's longer
    # runtime root. Each accepted connection represents one queued request;
    # reset/eof model its disposal while the previous one-shot service exits.
    path = tmp_path_factory.mktemp("handoff") / "broker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(path))
    path.chmod(0o600)
    listener.listen(8)
    listener.settimeout(0.05)
    stopped = threading.Event()
    state = SimpleNamespace(path=path, accepted=0, requests=[], errors=[])

    def serve() -> None:
        try:
            while not stopped.is_set():
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                index = state.accepted
                state.accepted += 1
                action = actions[index] if index < len(actions) else _frame(
                    wrappers._broker_json(_response())
                )
                with connection:
                    connection.settimeout(1)
                    if action == "reset":
                        # Closing with unread request data produces ECONNRESET
                        # on Linux AF_UNIX, like disposing a queued connection.
                        assert connection.recv(1, socket.MSG_PEEK)
                        continue
                    length = struct.unpack("!I", _receive(connection, 4))[0]
                    state.requests.append(json.loads(_receive(connection, length)))
                    if action == "eof":
                        continue
                    if action == "eof-unsafe-socket":
                        path.chmod(0o640)
                        continue
                    if action == "timeout":
                        stopped.wait(1)
                        continue
                    assert isinstance(action, bytes)
                    connection.sendall(action)
        except BaseException as exc:
            state.errors.append(exc)

    worker = threading.Thread(target=serve, name="one-shot-broker-fixture")
    worker.start()
    try:
        yield state
    finally:
        stopped.set()
        worker.join(timeout=2)
        listener.close()
        assert not worker.is_alive()
        assert state.errors == []


@pytest.mark.parametrize("disconnect", ("reset", "eof"))
def test_verify_survives_one_shot_service_handoff(
    tmp_path_factory: pytest.TempPathFactory, disconnect: str,
) -> None:
    with _one_shot_broker(
        tmp_path_factory, [disconnect, _frame(wrappers._broker_json(_response()))],
    ) as broker:
        assert wrappers._request_lan_preparation(
            socket_path=broker.path, timeout=2, restore_mode="verify",
        ) == VERIFY_RESULT
    assert broker.accepted == 2
    assert broker.requests
    assert all(request == {
        "operation": "prepare",
        "restore": {"mode": "verify", "generation": None},
        "schema": wrappers.PREPARER_BROKER_REQUEST_SCHEMA,
    } for request in broker.requests)


@pytest.mark.parametrize("disconnect", ("reset", "eof"))
def test_verify_stops_after_three_handoff_attempts(
    tmp_path_factory: pytest.TempPathFactory, disconnect: str,
) -> None:
    with _one_shot_broker(tmp_path_factory, [disconnect] * 4) as broker:
        with pytest.raises(wrappers.WrapperError, match="preparer broker request failed") as failure:
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=2, restore_mode="verify",
            )
    assert broker.accepted == 3
    if disconnect == "reset":
        assert isinstance(failure.value.__cause__, ConnectionResetError)
        assert failure.value.__cause__.errno == errno.ECONNRESET
    else:
        assert isinstance(failure.value.__cause__, EOFError)


@pytest.mark.parametrize("mode", ("none", "generation", "unreceipted"))
@pytest.mark.parametrize("disconnect", ("reset", "eof"))
def test_mutation_is_never_replayed_after_a_disconnect(
    tmp_path_factory: pytest.TempPathFactory, mode: str, disconnect: str,
) -> None:
    with _one_shot_broker(tmp_path_factory, [disconnect]) as broker:
        with pytest.raises(wrappers.WrapperError, match="preparer broker request failed"):
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=2, restore_mode=mode,
                generation="a" * 32 if mode == "generation" else None,
            )
    assert broker.accepted == 1


@pytest.mark.parametrize(
    "wire_response",
    (
        pytest.param(b"\0\0", id="partial-header-eof"),
        pytest.param(struct.pack("!I", 8) + b"{}", id="partial-body-eof"),
        pytest.param(struct.pack("!I", 0), id="empty-frame"),
        pytest.param(struct.pack("!I", wrappers.PREPARER_BROKER_MAX_FRAME_BYTES + 1), id="oversize-frame"),
        pytest.param(_frame(b"{bad-json}"), id="malformed-json"),
        pytest.param(_frame(b'{"ok":true,"ok":true}'), id="duplicate-json"),
        pytest.param(_frame(b" " + wrappers._broker_json(_response())), id="noncanonical-json"),
        pytest.param(_frame(wrappers._broker_json(_response(
            ok=False, error="request-refused", returncode=None,
        ))), id="authentication-refusal"),
        pytest.param(_frame(wrappers._broker_json(_response(
            ok=False, error="preparer-failed", returncode=1,
        ))), id="broker-operation-refusal"),
        pytest.param(_frame(wrappers._broker_json(_response(schema="foreign.v1"))), id="wrong-schema"),
        pytest.param(_frame(wrappers._broker_json(_response(result=None))), id="missing-result"),
        pytest.param(_frame(wrappers._broker_json(_response())) + b"x", id="trailing-bytes"),
    ),
)
def test_verify_does_not_retry_authentication_or_protocol_failures(
    tmp_path_factory: pytest.TempPathFactory, wire_response: bytes,
) -> None:
    with _one_shot_broker(tmp_path_factory, [wire_response]) as broker:
        with pytest.raises(wrappers.WrapperError, match="preparer broker"):
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=2, restore_mode="verify",
            )
    assert broker.accepted == 1


def test_verify_does_not_retry_a_response_timeout(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with _one_shot_broker(tmp_path_factory, ["timeout"]) as broker:
        with pytest.raises(wrappers.WrapperError) as failure:
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=0.05, restore_mode="verify",
            )
    assert broker.accepted == 1
    assert isinstance(failure.value.__cause__, TimeoutError)


def test_verify_retry_backoff_shares_the_original_timeout_budget(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    delays: list[float] = []

    def advance(delay: float) -> None:
        nonlocal elapsed
        delays.append(delay)
        elapsed += delay

    monkeypatch.setattr(wrappers, "time", SimpleNamespace(
        monotonic=lambda: elapsed, sleep=advance,
    ))
    with _one_shot_broker(tmp_path_factory, ["eof"] * 4) as broker:
        with pytest.raises(wrappers.WrapperError):
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=0.15, restore_mode="verify",
            )
    assert broker.accepted == 2
    assert delays == [0.1]


def test_verify_revalidates_socket_before_reconnecting(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with _one_shot_broker(tmp_path_factory, ["eof-unsafe-socket"]) as broker:
        with pytest.raises(wrappers.WrapperError, match="socket changed during verification handoff"):
            wrappers._request_lan_preparation(
                socket_path=broker.path, timeout=2, restore_mode="verify",
            )
    assert broker.accepted == 1
