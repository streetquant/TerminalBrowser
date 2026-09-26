from __future__ import annotations

import errno
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import termdeck.release_manager as release_manager
from termdeck import service_wrappers as wrappers


def _preparer_publication_result() -> dict[str, object]:
    def claim(path: str, mode: int) -> dict[str, object]:
        return {
            "path": path,
            "mode": mode,
            "identity": [1, 2, mode, 1, 3, 4, 5],
            "sha256": "a" * 64,
        }

    return {
        "schema": "termdeck.preparer-publication.v1",
        "operation": "publish",
        "ok": True,
        "generation": "b" * 32,
        "producer_receipt_sha256": "a" * 64,
        "receipt": claim("build-receipt.json", 0o644),
        "outputs": {
            path: claim(path, 0o600)
            for path in ("current-lan-ip", "nginx/allowed-hosts.map")
        },
        "tls": {
            "path": "tls",
            "identity": [1, 2, 0o700, os.geteuid()],
            "manifest": {
                name: claim(name, mode)
                for name, mode in {
                    "ca.crt": 0o644,
                    "ca.key": 0o600,
                    "server.key": 0o600,
                    "server.crt": 0o644,
                    "server-chain.crt": 0o644,
                    "server.ext": 0o600,
                }.items()
            },
        },
    }


def _serve_preparer_response(
    listener: socket.socket,
    observed: list[dict[str, object]],
    response: dict[str, object],
) -> None:
    connection, _address = listener.accept()
    with connection:
        header = connection.recv(4)
        assert len(header) == 4
        length = struct.unpack("!I", header)[0]
        payload = bytearray()
        while len(payload) < length:
            payload.extend(connection.recv(length - len(payload)))
        observed.append(json.loads(payload))
        encoded = wrappers._broker_json(response)
        connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _request_preparer_response(
    tmp_path_factory: pytest.TempPathFactory,
    response: dict[str, object],
    **request_options,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    # Keep the AF_UNIX pathname bounded under the authoritative runner's
    # private runtime root, independently of this descriptive test name.
    socket_path = tmp_path_factory.mktemp("broker") / "preparer.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    observed: list[dict[str, object]] = []
    worker = threading.Thread(
        target=_serve_preparer_response,
        args=(listener, observed, response),
    )
    worker.start()
    try:
        result = wrappers._request_lan_preparation(
            socket_path=socket_path, timeout=2.0, **request_options,
        )
    finally:
        worker.join(timeout=5)
        listener.close()
    assert worker.is_alive() is False
    return result, observed


def _preparer_success_response() -> dict[str, object]:
    return {
        "error": None,
        "ok": True,
        "returncode": 0,
        "schema": wrappers.PREPARER_BROKER_RESPONSE_SCHEMA,
        "result": _preparer_publication_result(),
    }


@pytest.mark.parametrize("optional", ("none", "serial", "cleanup-warnings"))
def test_lan_preparer_client_uses_closed_root_broker_protocol(
    tmp_path_factory: pytest.TempPathFactory, optional: str,
) -> None:
    response = _preparer_success_response()
    if optional == "serial":
        result = response["result"]
        serial = dict(result["tls"]["manifest"]["ca.crt"], path="ca.srl")
        result["tls"]["manifest"]["ca.srl"] = serial
    elif optional == "cleanup-warnings":
        response["result"]["cleanup_warnings"] = ["retained private transaction backup"]
    result, observed = _request_preparer_response(tmp_path_factory, response)
    assert result is None
    assert observed == [
        {
            "operation": "prepare",
            "restore": {"generation": None, "mode": "none"},
            "schema": wrappers.PREPARER_BROKER_REQUEST_SCHEMA,
        }
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    (
        pytest.param(("result",), None, id="null-result"),
        pytest.param(("result",), {}, id="empty-result"),
        pytest.param(("result", "schema"), "foreign.v1", id="foreign-schema"),
        pytest.param(("result", "operation"), "verify", id="wrong-operation"),
        pytest.param(("result", "ok"), 1, id="numeric-success"),
        pytest.param(("result", "generation"), "A" * 32, id="invalid-generation"),
        pytest.param(("result", "foreign"), True, id="unknown-result-field"),
        pytest.param(("result", "producer_receipt_sha256"), "b" * 64, id="foreign-producer-receipt"),
        pytest.param(("result", "receipt"), {}, id="empty-receipt"),
        pytest.param(("result", "receipt", "path"), "foreign.json", id="foreign-receipt-path"),
        pytest.param(("result", "receipt", "mode"), 0o600, id="receipt-mode"),
        pytest.param(("result", "receipt", "identity"), [1, 2, 0o644, 1], id="short-receipt-identity"),
        pytest.param(("result", "receipt", "identity"), [1, True, 0o644, 1, 3, 4, 5], id="boolean-identity"),
        pytest.param(("result", "receipt", "identity"), [1, 0, 0o644, 1, 3, 4, 5], id="zero-inode"),
        pytest.param(("result", "receipt", "identity"), [1, 2, 0o644, 2, 3, 4, 5], id="hardlinked-receipt"),
        pytest.param(("result", "receipt", "sha256"), "A" * 64, id="invalid-receipt-digest"),
        pytest.param(("result", "receipt", "extra"), True, id="unknown-receipt-field"),
        pytest.param(("result", "outputs"), {}, id="missing-outputs"),
        pytest.param(("result", "outputs", "foreign"), {}, id="foreign-output"),
        pytest.param(("result", "outputs", "current-lan-ip", "path"), "tls/ca.key", id="foreign-output-path"),
        pytest.param(("result", "tls", "path"), "../tls", id="foreign-tls-path"),
        pytest.param(("result", "tls", "identity"), [1, 2, 0o700, os.geteuid() + 1], id="foreign-tls-owner"),
        pytest.param(("result", "tls", "identity"), [1, 2, 0o755, os.geteuid()], id="tls-mode"),
        pytest.param(("result", "tls", "manifest"), {}, id="missing-tls-leaves"),
        pytest.param(("result", "tls", "manifest", "foreign"), {}, id="foreign-tls-leaf"),
        pytest.param(("result", "tls", "manifest", "server.key", "mode"), 0o644, id="public-private-key"),
        pytest.param(("result", "cleanup_warnings"), "warning", id="invalid-cleanup-warnings"),
        pytest.param(("result", "cleanup_warnings"), [False], id="invalid-cleanup-warning"),
        pytest.param(("result", "cleanup_warnings"), ["warning"] * 9, id="too-many-cleanup-warnings"),
        pytest.param(("result", "cleanup_warnings"), ["w" * 513], id="long-cleanup-warning"),
        pytest.param(("result", "cleanup_warnings"), ["\u2603" * 512] * 2, id="large-encoded-cleanup-warnings"),
        pytest.param(("returncode",), False, id="boolean-returncode"),
        pytest.param(("returncode",), 0.0, id="float-returncode"),
        pytest.param(("extra",), True, id="unknown-response-field"),
    ),
)
def test_lan_preparer_client_rejects_invalid_publication_response(
    tmp_path_factory: pytest.TempPathFactory,
    path: tuple[str, ...],
    value: object,
) -> None:
    response = _preparer_success_response()
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(wrappers.WrapperError, match="preparer broker"):
        _request_preparer_response(tmp_path_factory, response)


def test_lan_preparer_client_requires_current_publication_result(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    response = _preparer_success_response()
    del response["result"]
    # A legacy preparation acknowledgement cannot prove that the current
    # separate producer-to-consumer publication completed.
    with pytest.raises(wrappers.WrapperError, match="unexpected preparation result"):
        _request_preparer_response(tmp_path_factory, response)


@pytest.mark.parametrize(
    ("expected_operation", "result_operation", "accepted"),
    (
        ("verify", "verify", True),
        ("verify", "publish", False),
        ("publish", "verify", False),
        ("restore", "restore", False),
    ),
)
def test_preparer_publication_result_requires_the_expected_operation(
    expected_operation: str, result_operation: str, accepted: bool,
) -> None:
    result = _preparer_publication_result()
    result["operation"] = result_operation
    if accepted:
        assert wrappers._validate_preparer_publication_result(result, operation=expected_operation) is None
    else:
        with pytest.raises(wrappers.WrapperError, match="publication"):
            wrappers._validate_preparer_publication_result(result, operation=expected_operation)


def test_lan_preparer_client_preserves_restore_result_for_receipt_validation(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    response = _preparer_success_response()
    response["result"] = {"receipt": {"fixture": True}, "publication": {"operation": "verify"}}
    result, observed = _request_preparer_response(
        tmp_path_factory, response, restore_mode="verify",
    )
    assert result == response["result"]
    assert observed[0]["restore"] == {"generation": None, "mode": "verify"}


def test_lan_preparer_client_accepts_actual_broker_publication_over_socket(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_source = Path(__file__).resolve().parents[1] / "deploy" / "termdeck-preparer-broker"
    module_name = "termdeck_preparer_broker_wrapper_wire_test"
    loader = importlib.machinery.SourceFileLoader(module_name, str(broker_source))
    spec = importlib.util.spec_from_loader(module_name, loader)
    assert spec is not None
    broker = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, broker)
    loader.exec_module(broker)

    libexec = tmp_path / "libexec"
    libexec.mkdir()
    images = {
        name: libexec / name
        for name in (
            "termdeck-preparer-broker",
            "termdeck-preparer-publication",
            "termdeck-web-prepare-runtime",
        )
    }
    for name, path in images.items():
        path.write_bytes(broker_source.read_bytes() if name == "termdeck-preparer-broker" else b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    socket_path = tmp_path_factory.mktemp("wire") / "request.sock"
    config = broker.BrokerConfig(
        broker_path=images["termdeck-preparer-broker"],
        publication_path=images["termdeck-preparer-publication"],
        preparer_path=images["termdeck-web-prepare-runtime"],
        runtime_root=tmp_path / "producer",
        consumer_root=tmp_path / "consumer",
        socket_path=socket_path,
        root_uid=os.getuid(),
        root_gid=os.getgid(),
        request_timeout=2.0,
    )
    principals = broker.Principals(
        preparer_uid=config.root_uid + 100,
        preparer_gid=config.root_gid + 100,
        caller_uid=config.root_uid + 200,
        caller_gid=config.root_gid + 200,
    )
    # This unprivileged fixture exercises the real transport, broker dispatch,
    # helper-result parsing and wrapper. Principal separation itself is covered
    # by the broker's dedicated tests; no installed executable is launched here.
    monkeypatch.setattr(broker, "_validate_socket_path", lambda *_args, **_kwargs: None)
    operations: list[str] = []

    def run_fixture_process(command, **kwargs):
        executable = Path(os.readlink(command[0]))
        if executable.samefile(config.publication_path):
            operation = command[1]
            operations.append(operation)
            result = _preparer_publication_result() if operation == "publish" else {
                "schema": broker.PUBLICATION_SCHEMA,
                "operation": "seed",
                "ok": True,
            }
            return SimpleNamespace(returncode=0, stdout=broker._canonical_json(result), stderr=b"")
        assert executable.samefile(config.preparer_path)
        assert command[1:] == ()
        operations.append("prepare")
        return SimpleNamespace(returncode=0, stdout=None, stderr=b"")

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    outcomes: list[int] = []
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            outcomes.append(broker.serve_once(
                listener,
                config=config,
                resolve_principals=lambda _config: principals,
                peer_credentials=lambda _connection: broker.PeerCredentials(
                    os.getpid(), principals.caller_uid, principals.caller_gid,
                ),
                run_process=run_fixture_process,
            ))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        assert wrappers._request_lan_preparation(socket_path=socket_path, timeout=2.0) is None
    finally:
        worker.join(timeout=5)
        listener.close()
        if failures:
            raise failures[0]
    assert not worker.is_alive()
    assert failures == []
    assert outcomes == [0]
    assert operations == ["seed", "prepare", "publish"]


def test_lan_preflight_routes_preparation_through_broker(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-lan.service"]
    layout = wrappers.resolve_layout(**layout_dirs)
    executable_checks: list[Path] = []
    calls: list[object] = []
    monkeypatch.setattr(
        wrappers,
        "_regular_executable",
        lambda path, **_kwargs: executable_checks.append(Path(path)),
    )
    monkeypatch.setattr(wrappers, "_regular_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrappers, "build_start_command", lambda *_args, **_kwargs: ("nginx",))
    monkeypatch.setattr(
        wrappers,
        "_request_lan_preparation",
        lambda **kwargs: calls.append(("broker", kwargs)),
    )
    monkeypatch.setattr(
        wrappers.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(tuple(command)),
    )

    assert wrappers._preflight_unit_start(spec, layout) == ("nginx",)
    assert executable_checks == [Path("/usr/bin/nginx")]
    assert calls[0][0] == "broker"
    assert calls[0][1]["runtime_root"] == layout.runtime_root / "termdeck-lan"
    assert calls[1][0:2] == ("/usr/bin/nginx", "-t")


def test_backend_admission_exports_persistent_shared_runtime(layout_dirs, monkeypatch):
    from termdeck import storage_fabric

    layout = wrappers.resolve_layout(**layout_dirs)
    root = layout.state_root / "tmux"
    root.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(storage_fabric, "require_storage_fabric", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(storage_fabric, "tmux_subprocess_environment", lambda **kw: {"TMUX_TMPDIR": str(root)})
    for name in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TMUX_TMPDIR", "TERMDECK_TMUX_IDENTITY", "TMUX"):
        monkeypatch.setenv(name, "foreign")
    wrappers._admit_backend_tmux_runtime(layout)
    assert calls[0][1]["production"] is True
    assert calls[0][1]["tmux_runtime"] == root
    assert calls[0][1]["db_path"] == layout.state_root / "sessions.sqlite3"
    assert os.environ["TMUX_TMPDIR"] == str(root)
    assert os.environ["TERMDECK_TMUX_RUNTIME"] == str(root)
    assert os.environ["TERMDECK_TMUX_IDENTITY"] == f"{root.stat().st_dev}:{root.stat().st_ino}:{os.geteuid()}"
    assert "TMUX" not in os.environ


def test_backend_admission_failure_prevents_export(layout_dirs, monkeypatch):
    from termdeck import storage_fabric

    def reject(*args, **kwargs):
        raise storage_fabric.StorageFabricError("unadmitted storage")

    monkeypatch.setattr(storage_fabric, "require_storage_fabric", reject)
    monkeypatch.setenv("TMUX_TMPDIR", "foreign")
    with pytest.raises(wrappers.WrapperError, match="admission failed"):
        wrappers._admit_backend_tmux_runtime(wrappers.resolve_layout(**layout_dirs))
    assert os.environ["TMUX_TMPDIR"] == "foreign"


@pytest.fixture()
def layout_dirs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "source_root": tmp_path / "source",
        # Disposable test roots explicitly name a sibling control namespace;
        # it exercises immutable wrapper metadata without pretending a
        # same-UID temporary directory is production attestation authority.
        "control_root": tmp_path / "control",
        "runtime_root": tmp_path / "runtime",
        "state_root": tmp_path / "state",
        "managed_root": tmp_path / "managed",
        "managed_bin": tmp_path / "bin",
        "unit_dir": tmp_path / "units",
    }
    for path in paths.values():
        path.mkdir()
    return paths


def _published(paths: dict[str, Path], release: str = "release-a") -> dict[str, object]:
    return wrappers.publish_wrappers(release, **paths)


def _immutable_execution_site(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Create the frozen wheel-site modules used by an immutable wrapper."""

    site = tmp_path / "selected-release-site"
    package = site / "termdeck"
    package.mkdir(parents=True)
    modules = {
        "termdeck/__init__.py": b"# selected immutable package\n",
        "termdeck/service_wrappers.py": (
            b"def main(*, unit, action, **kwargs):\n"
            b"    return 0\n"
        ),
        "termdeck/web_activation.py": b"# selected immutable recovery module\n",
        "termdeck/storage_fabric.py": b"# selected immutable storage module\n",
    }
    binding: dict[str, object] = {}
    for relative, content in modules.items():
        path = site / relative
        path.write_bytes(content)
        path.chmod(0o444)
        binding[relative] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    package.chmod(0o555)
    site.chmod(0o555)
    return site, binding


def test_immutable_execution_binding_uses_selected_release_site(
    layout_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A generated wrapper never imports policy from the mutable checkout."""

    site, binding = _immutable_execution_site(tmp_path)
    mutable_package = layout_dirs["source_root"] / "termdeck"
    mutable_package.mkdir()
    (mutable_package / "service_wrappers.py").write_text(
        "raise RuntimeError('mutable checkout was imported')\n"
    )
    rendered = wrappers.render_wrappers(
        "release-a",
        **layout_dirs,
        execution_root=site,
        execution_binding=binding,
    )
    wrapper = rendered[layout_dirs["managed_root"] / "termdeck-web-backend"]
    assert f"sys.path.insert(0, {json.dumps(str(site))})".encode() in wrapper
    assert f"sys.path.insert(0, {json.dumps(str(layout_dirs['source_root']))})".encode() not in wrapper
    assert (
        f"os.environ[{json.dumps(wrappers.CONTROL_ROOT_ENV)}] = "
        f"{json.dumps(str(layout_dirs['control_root']))}"
    ).encode() in wrapper
    manifest = json.loads(rendered.manifest)
    assert manifest["execution_root"] == str(site)
    assert manifest["execution_binding"] == binding
    assert manifest["control_root"] == str(layout_dirs["control_root"])
    assert manifest["control_socket"] == str(
        layout_dirs["control_root"] / wrappers.CONTROL_ATTEST_SOCKET_NAME
    )
    assert manifest["control_attestation"] == {
        "socket": str(layout_dirs["control_root"] / wrappers.CONTROL_ATTEST_SOCKET_NAME),
        "operation": wrappers.CONTROL_ATTEST_OPERATION,
        "request_schema": wrappers.CONTROL_ATTEST_REQUEST_SCHEMA,
        "response_schema": wrappers.CONTROL_ATTEST_RESPONSE_SCHEMA,
        "producer_principal": wrappers.CONTROL_ATTEST_PRODUCER,
        "excluded_principal": wrappers.CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
        "control_root_owner": wrappers.CONTROL_ATTEST_OWNER,
        "control_root_group": wrappers.CONTROL_ATTEST_GROUP,
        "control_root_mode": wrappers.CONTROL_ROOT_MODE,
        "socket_owner": wrappers.CONTROL_ATTEST_OWNER,
        "socket_group": wrappers.CONTROL_ATTEST_GROUP,
        "socket_mode": wrappers.CONTROL_ATTEST_SOCKET_MODE,
    }

    wrappers.publish_wrappers(
        rendered,
    )
    checked = wrappers.readback_wrappers(
        **layout_dirs,
        execution_root=site,
        execution_binding=binding,
        expected_release_id="release-a",
    )
    assert checked["execution_root"] == str(site)
    assert checked["execution_binding"] == binding
    assert checked["control_root"] == str(layout_dirs["control_root"])
    assert checked["control_attestation"] == manifest["control_attestation"]

    completed = subprocess.run(
        [str(layout_dirs["managed_root"] / "termdeck-web-backend")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_recovery_command_runs_module_from_selected_execution_root(
    layout_dirs: dict[str, Path], tmp_path: Path
) -> None:
    site, binding = _immutable_execution_site(tmp_path)
    command = wrappers.build_start_command(
        "termdeck-web-recovery.service",
        **layout_dirs,
        execution_root=site,
        execution_binding=binding,
    )
    assert command[:4] == ("/usr/bin/python3", "-I", "-B", "-c")
    assert str(site) in command
    assert not any("termdeck-web-activate.py" in item for item in command)


def test_immutable_fixture_requires_an_explicit_isolated_control_root(
    layout_dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A frozen wrapper cannot silently inherit a max-owned runtime parent."""

    site, binding = _immutable_execution_site(tmp_path)
    without_control = dict(layout_dirs)
    del without_control["control_root"]
    with pytest.raises(wrappers.WrapperError, match="explicit control root"):
        wrappers.resolve_layout(
            **without_control,
            execution_root=site,
            execution_binding=binding,
        )


def test_start_wrapper_replaces_caller_control_root_metadata(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every start path installs the wrapper-bound endpoint before dispatch."""

    _published(layout_dirs)
    monkeypatch.setenv(wrappers.CONTROL_ROOT_ENV, "/tmp/attacker-control")
    assert wrappers.run_wrapper(
        "termdeck-checkpoint.service",
        "start",
        release_id="release-a",
        **layout_dirs,
    ) == 0
    assert os.environ[wrappers.CONTROL_ROOT_ENV] == str(layout_dirs["control_root"])


def test_lan_dispatch_inherits_the_bound_control_root(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LAN preparation dispatch sees the immutable endpoint metadata."""

    _published(layout_dirs)
    observed: dict[str, str] = {}

    def preflight(spec: wrappers.UnitSpec, _layout: wrappers.WrapperLayout) -> tuple[str, ...]:
        assert spec.kind == "lan"
        observed["control_root"] = os.environ[wrappers.CONTROL_ROOT_ENV]
        return ("/usr/bin/true",)

    class StopExec(Exception):
        pass

    def stop_execv(_program: str, _argv: list[str]) -> None:
        raise StopExec()

    monkeypatch.setenv(wrappers.CONTROL_ROOT_ENV, "/tmp/attacker-control")
    monkeypatch.setattr(wrappers, "_preflight_unit_start", preflight)
    monkeypatch.setattr(wrappers, "_record_start_identity", lambda *_args: None)
    monkeypatch.setattr(wrappers.os, "execv", stop_execv)
    with pytest.raises(StopExec):
        wrappers.run_wrapper(
            "termdeck-web-lan.service",
            "start",
            release_id="release-a",
            **layout_dirs,
        )
    assert observed == {"control_root": str(layout_dirs["control_root"])}


@pytest.mark.parametrize("inherited", [None, "/tmp/stale-termdeck-state"])
def test_recovery_dispatch_inherits_its_explicit_state_root(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, inherited: str | None
) -> None:
    monkeypatch.setattr(wrappers.os, "environ", dict(os.environ))
    _published(layout_dirs)
    if inherited is None:
        monkeypatch.delenv("TERMDECK_STATE_ROOT", raising=False)
    else:
        monkeypatch.setenv("TERMDECK_STATE_ROOT", inherited)
    observed: dict[str, str] = {}

    def preflight(spec: wrappers.UnitSpec, layout: wrappers.WrapperLayout) -> tuple[str, ...]:
        assert spec.kind == "recovery"
        observed["state"] = os.environ["TERMDECK_STATE_ROOT"]
        command = wrappers.build_start_command(spec, **layout_dirs)
        assert command[command.index("--database") + 1] == str(
            Path(observed["state"]) / "sessions.sqlite3"
        )
        return command

    class StopExec(Exception):
        pass

    def stop_execv(_program: str, _argv: list[str]) -> None:
        assert os.environ["TERMDECK_STATE_ROOT"] == str(layout_dirs["state_root"])
        raise StopExec()

    monkeypatch.setattr(wrappers, "_preflight_unit_start", preflight)
    monkeypatch.setattr(wrappers, "_record_start_identity", lambda *_args: None)
    monkeypatch.setattr(wrappers.os, "execv", stop_execv)
    with pytest.raises(StopExec):
        wrappers.run_wrapper(
            "termdeck-web-recovery.service", "start", release_id="release-a", **layout_dirs
        )
    assert observed["state"] == str(layout_dirs["state_root"])


def test_readback_rejects_a_rehashed_attestation_endpoint_change(
    layout_dirs: dict[str, Path],
) -> None:
    """A writable manifest cannot redirect preparation to another signer."""

    _published(layout_dirs)
    manifest_path = layout_dirs["managed_root"] / wrappers.WRAPPER_MANIFEST_NAME
    payload = json.loads(manifest_path.read_text())
    attestation = payload["control_attestation"]
    assert isinstance(attestation, dict)
    attestation["producer_principal"] = "max"
    del payload["manifest_sha256"]
    payload["manifest_sha256"] = wrappers._sha256_bytes(wrappers._canonical_json(payload))
    manifest_path.write_bytes(wrappers._canonical_json(payload))
    manifest_path.chmod(wrappers.UNIT_CONFIG_MODE)

    with pytest.raises(wrappers.WrapperError, match="control attestation"):
        wrappers.readback_wrappers(**layout_dirs, expected_release_id="release-a")


def test_publication_rejects_a_control_root_inside_runtime_tree(
    layout_dirs: dict[str, Path],
) -> None:
    """The max-owned runtime tree cannot become its own signing authority."""

    nested_control = layout_dirs["runtime_root"] / "control"
    nested_control.mkdir()
    unsafe_layout = dict(layout_dirs, control_root=nested_control)
    with pytest.raises(wrappers.WrapperError, match="isolated from the runtime"):
        wrappers.publish_wrappers("release-a", **unsafe_layout)
    assert list(layout_dirs["managed_root"].iterdir()) == []
    assert list(layout_dirs["state_root"].iterdir()) == []


def _production_control_binding_kwargs(
    layout_dirs: dict[str, Path], tmp_path: Path
) -> tuple[dict[str, object], Path, Path]:
    site, binding = _immutable_execution_site(tmp_path)
    return (
        {
            **layout_dirs,
            "execution_root": site,
            "execution_binding": binding,
        },
        layout_dirs["runtime_root"],
        layout_dirs["control_root"],
    )


def _patch_production_control_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_root: Path,
    control_root: Path,
    mode: int = 0o710,
    group_id: int = 4242,
    max_groups: list[int] | None = None,
) -> None:
    """Model the root-owned production endpoint without chowning test files."""

    original_lstat = Path.lstat

    def fake_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        if path == control_root:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | mode,
                st_uid=0,
                st_gid=group_id,
            )
        return original_lstat(path)

    monkeypatch.setattr(wrappers, "CANONICAL_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(wrappers, "CANONICAL_CONTROL_ROOT", control_root)
    monkeypatch.setattr(wrappers, "_assert_canonical", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(
        wrappers.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=4242),
    )
    monkeypatch.setattr(
        wrappers.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_gid=1000),
    )
    monkeypatch.setattr(
        wrappers.os,
        "getgrouplist",
        lambda _name, _gid: [1000] if max_groups is None else max_groups,
    )


def test_immutable_production_control_root_requires_exact_group_mode(
    layout_dirs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, runtime_root, control_root = _production_control_binding_kwargs(layout_dirs, tmp_path)
    _patch_production_control_identity(
        monkeypatch,
        runtime_root=runtime_root,
        control_root=control_root,
    )

    binding = wrappers.control_attestation_binding(**kwargs)
    assert binding["control_root_owner"] == "root"
    assert binding["control_root_group"] == "termdeck-preparer"
    assert binding["control_root_mode"] == 0o710
    assert binding["socket_mode"] == 0o660


@pytest.mark.parametrize(
    ("mode", "group_id", "max_groups", "message"),
    (
        (0o700, 4242, None, "root:termdeck-preparer mode 0710"),
        (0o710, 4243, None, "root:termdeck-preparer mode 0710"),
        (0o710, 4242, [1000, 4242], "may not join termdeck-preparer"),
    ),
)
def test_immutable_production_control_root_rejects_bad_boundary(
    layout_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    group_id: int,
    max_groups: list[int] | None,
    message: str,
) -> None:
    kwargs, runtime_root, control_root = _production_control_binding_kwargs(layout_dirs, tmp_path)
    _patch_production_control_identity(
        monkeypatch,
        runtime_root=runtime_root,
        control_root=control_root,
        mode=mode,
        group_id=group_id,
        max_groups=max_groups,
    )

    with pytest.raises(wrappers.WrapperError, match=message):
        wrappers.control_attestation_binding(**kwargs)


def test_immutable_production_control_root_requires_group_resolution(
    layout_dirs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, runtime_root, control_root = _production_control_binding_kwargs(layout_dirs, tmp_path)
    _patch_production_control_identity(
        monkeypatch,
        runtime_root=runtime_root,
        control_root=control_root,
    )

    def missing_group(_name: str) -> SimpleNamespace:
        raise KeyError(_name)

    monkeypatch.setattr(wrappers.grp, "getgrnam", missing_group)
    with pytest.raises(wrappers.WrapperError, match="control group termdeck-preparer is unavailable"):
        wrappers.control_attestation_binding(**kwargs)


def test_render_is_deterministic_and_contains_exactly_twelve_wrappers(
    layout_dirs: dict[str, Path],
) -> None:
    first = wrappers.render_wrappers("release-a", **layout_dirs)
    second = wrappers.render_wrappers("release-a", **layout_dirs)

    assert len(first) == 12
    assert {path.name for path in first} == set(wrappers.WRAPPER_NAMES)
    assert dict(first) == dict(second)
    assert first.configs == second.configs
    assert first.manifest == second.manifest
    for path, content in first.items():
        assert path.parent == layout_dirs["managed_root"]
        assert content.startswith(b"#!/usr/bin/python3\n")


def test_publish_readback_modes_and_release_binding(layout_dirs: dict[str, Path]) -> None:
    result = _published(layout_dirs)
    managed = layout_dirs["managed_root"]
    state = layout_dirs["state_root"]

    assert result["ok"] is True
    assert {entry.name for entry in managed.iterdir() if entry.name in wrappers.WRAPPER_NAMES} == set(
        wrappers.WRAPPER_NAMES
    )
    for name in wrappers.WRAPPER_NAMES:
        path = managed / name
        assert stat.S_ISREG(path.lstat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == wrappers.WRAPPER_MODE
    for name in wrappers.UNIT_CONFIG_NAMES:
        assert stat.S_IMODE((state / name).stat().st_mode) == wrappers.UNIT_CONFIG_MODE

    readback = wrappers.readback_wrappers(**layout_dirs, expected_release_id="release-a")
    assert readback["release_id"] == "release-a"
    with pytest.raises(wrappers.WrapperError):
        wrappers.readback_wrappers(**layout_dirs, expected_release_id="release-b")


def test_capture_snapshot_retains_complete_anchored_leaf_identity(
    layout_dirs: dict[str, Path],
) -> None:
    _published(layout_dirs)
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    item = wrappers.capture_wrapper_snapshot(**layout_dirs)[target]
    info = target.stat()

    assert item.exists is True
    assert item.device == info.st_dev
    assert item.inode == info.st_ino
    assert item.nlink == 1
    assert item.size == len(item.content)
    assert item.ctime_ns == info.st_ctime_ns
    assert item.sha256 == wrappers._sha256_bytes(item.content)


def test_wrapper_preimage_payload_retains_complete_anchored_leaf_identity(
    layout_dirs: dict[str, Path],
) -> None:
    """Persisted release receipts retain the identity captured by wrappers."""

    _published(layout_dirs)
    snapshot = wrappers.capture_wrapper_snapshot(**layout_dirs)
    paths = tuple(sorted(snapshot, key=str))
    payload = release_manager._wrapper_snapshot_payload(
        snapshot,
        kwargs=layout_dirs,
        paths=paths,
    )
    restored = release_manager._wrapper_snapshot_from_payload(
        payload,
        kwargs=layout_dirs,
        paths=paths,
    )
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    expected = snapshot[target]
    observed = restored[target]

    assert observed["device"] == expected.device
    assert observed["inode"] == expected.inode
    assert observed["nlink"] == expected.nlink == 1
    assert observed["size"] == expected.size
    assert observed["mtime_ns"] == expected.mtime_ns
    assert observed["ctime_ns"] == expected.ctime_ns
    assert observed["sha256"] == expected.sha256

    legacy = {
        path: {"exists": item.exists, "content": item.content, "mode": item.mode}
        for path, item in snapshot.items()
    }
    with pytest.raises(release_manager.ReleaseError, match="lost its anchored identity"):
        release_manager._wrapper_snapshot_payload(
            legacy,
            kwargs=layout_dirs,
            paths=paths,
        )


def test_restore_rejects_malformed_complete_snapshot_identity(
    layout_dirs: dict[str, Path],
) -> None:
    _published(layout_dirs)
    snapshot = wrappers.capture_wrapper_snapshot(**layout_dirs)
    path = next(iter(snapshot))
    item = snapshot[path]
    malformed = dict(snapshot)
    malformed[path] = {
        "exists": item.exists,
        "content": item.content,
        "mode": item.mode,
        "device": "not-an-integer",
        "inode": item.inode,
        "nlink": item.nlink,
        "size": item.size,
        "mtime_ns": item.mtime_ns,
        "ctime_ns": item.ctime_ns,
        "sha256": item.sha256,
    }

    with pytest.raises(wrappers.WrapperError, match="device"):
        wrappers.restore_wrappers(
            malformed,
            _parent_guards=snapshot.parent_guards,
            **layout_dirs,
        )


def test_restore_accepts_a_complete_legacy_content_only_snapshot(
    layout_dirs: dict[str, Path],
) -> None:
    """Old complete receipts remain valid desired-state preimages."""

    _published(layout_dirs, "release-a")
    captured = wrappers.capture_wrapper_snapshot(**layout_dirs)
    legacy = {
        path: {"exists": item.exists, "content": item.content, "mode": item.mode}
        for path, item in captured.items()
    }
    _published(layout_dirs, "release-b")

    result = wrappers.restore_wrappers(
        legacy,
        _parent_guards=captured.parent_guards,
        **layout_dirs,
    )

    assert result["ok"] is True
    assert wrappers.readback_wrappers(**layout_dirs, expected_release_id="release-a")["release_id"] == "release-a"


def test_publication_carries_live_identity_into_new_release(
    layout_dirs: dict[str, Path],
) -> None:
    _published(layout_dirs, "release-a")
    path = layout_dirs["state_root"] / "termdeck-web-backend.service.conf"
    config = json.loads(path.read_text())
    config.update(
        {
            "pid": 1234,
            "start_ticks": 5678,
            "exe": "/usr/bin/termdeck-web-server",
            "argv": ["/usr/bin/termdeck-web-server", "--db"],
            "command_sha256": "identity-sha",
            "state": "starting",
        }
    )
    path.write_bytes((json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.chmod(path, wrappers.UNIT_CONFIG_MODE)

    _published(layout_dirs, "release-b")
    updated = json.loads(path.read_text())
    assert updated["release_id"] == "release-b"
    assert updated["pid"] == 1234
    assert updated["start_ticks"] == 5678
    assert updated["state"] == "starting"


def test_preflight_rejects_symlinked_managed_root(layout_dirs: dict[str, Path]) -> None:
    target = layout_dirs["managed_root"]
    target.rmdir()
    link = target.with_name("managed-link")
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    changed = dict(layout_dirs, managed_root=link)

    with pytest.raises(wrappers.WrapperError):
        wrappers.publish_wrappers("release-a", **changed)


def test_render_rejects_noncanonical_path(layout_dirs: dict[str, Path]) -> None:
    with pytest.raises(wrappers.WrapperError):
        wrappers.render_wrappers(
            "release-a",
            **dict(layout_dirs, managed_root=layout_dirs["managed_root"] / ".." / "managed"),
        )


def test_command_contract(layout_dirs: dict[str, Path]) -> None:
    backend = wrappers.build_start_command("termdeck-web-backend.service", **layout_dirs)
    assert backend[0] == str(layout_dirs["runtime_root"] / "termdeck-lan/bin/termdeck-web-server")
    assert backend[-1] == "120000"
    lan = wrappers.build_start_command("termdeck-web-lan.service", **layout_dirs)
    assert lan[:2] == ("/usr/bin/nginx", "-p")
    assert lan[-2:] == ("-g", "daemon off;")
    recovery = wrappers.build_start_command("termdeck-web-recovery.service", **layout_dirs)
    assert recovery[0:3] == ("/usr/bin/python3", "-I", "-B")
    assert recovery[-2:] == ("--unit-dir", str(layout_dirs["unit_dir"]))
    cloudflared = wrappers.build_start_command("termdeck-cloudflared.service", **layout_dirs)
    assert cloudflared[-3:] == ("--no-autoupdate", "tunnel", "run")
    checkpoint = wrappers.build_stop_command("termdeck-checkpoint.service", **layout_dirs)
    assert checkpoint[-3:] == (str(layout_dirs["managed_bin"] / "t1000"), "checkpoint", "--all")
    assert f"HOME={layout_dirs['state_root']}" in checkpoint


def test_process_identity_uses_start_ticks_and_argv() -> None:
    identity = wrappers.process_identity(os.getpid())
    assert identity["start_ticks"] == wrappers.proc_start_ticks(os.getpid())
    assert wrappers.verify_process_identity(identity)
    stale = dict(identity, start_ticks=int(identity["start_ticks"]) + 1)
    assert wrappers.verify_process_identity(stale) is False


def test_checkpoint_start_is_a_noop(layout_dirs: dict[str, Path]) -> None:
    _published(layout_dirs)
    configs_before = {
        path.name: path.read_bytes()
        for path in layout_dirs["state_root"].glob("*.service.conf")
    }
    assert wrappers.run_wrapper(
        "termdeck-checkpoint.service",
        "start",
        release_id="release-a",
        **layout_dirs,
    ) == 0
    configs_after = {
        path.name: path.read_bytes()
        for path in layout_dirs["state_root"].glob("*.service.conf")
    }
    assert configs_after == configs_before


def test_checkpoint_stop_runs_canonical_launcher(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class FinishedProcess:
        pid = 1234

        def wait(self, *, timeout: float) -> int:
            assert timeout == wrappers.CHECKPOINT_KILL_GRACE
            return 0

    def fake_popen(command: tuple[str, ...], **kwargs: object) -> FinishedProcess:
        calls.append((command, kwargs))
        return FinishedProcess()

    monkeypatch.setattr(wrappers.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wrappers, "_wait_checkpoint_exit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrappers, "_checkpoint_group_is_running", lambda _pid: False)
    assert wrappers.stop_unit("termdeck-checkpoint.service", **layout_dirs) == 0
    assert calls
    command, kwargs = calls[0]
    assert command[-3:] == (str(layout_dirs["managed_bin"] / "t1000"), "checkpoint", "--all")
    assert kwargs == {"start_new_session": True}


def test_checkpoint_timeout_terminates_its_process_group(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    signals: list[tuple[int, int]] = []

    class TimedOutProcess:
        pid = 4321

        def wait(self, *, timeout: float) -> int:
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("checkpoint", timeout)
            return -int(signal.SIGTERM)

    monkeypatch.setattr(wrappers.subprocess, "Popen", lambda *_args, **_kwargs: TimedOutProcess())
    monkeypatch.setattr(
        wrappers, "_wait_checkpoint_exit", lambda child, **kwargs: child.wait(**kwargs)
    )
    monkeypatch.setattr(wrappers.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    assert wrappers.stop_unit("termdeck-checkpoint.service", **layout_dirs) == 124
    assert signals == [(4321, signal.SIGTERM)]
    assert waits == [wrappers.CHECKPOINT_TIMEOUT, wrappers.CHECKPOINT_TERMINATE_GRACE]


def test_stale_pid_stop_is_idempotent(layout_dirs: dict[str, Path]) -> None:
    _published(layout_dirs)
    path = layout_dirs["state_root"] / "termdeck-web-backend.service.conf"
    config = json.loads(path.read_text())
    config.update(
        {
            "pid": 999_999_999,
            "start_ticks": 1,
            "exe": "/usr/bin/false",
            "argv": ["/usr/bin/false"],
            "command_sha256": "x",
            "state": "starting",
        }
    )
    path.write_text(json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, wrappers.UNIT_CONFIG_MODE)
    assert wrappers.stop_unit("termdeck-web-backend.service", release_id="release-a", **layout_dirs) == 0
    cleared = json.loads(path.read_text())
    assert cleared["pid"] is None
    assert cleared["state"] == "stopped"


def test_failed_publication_restores_preimages(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_readback(**_: object) -> dict[str, object]:
        raise wrappers.WrapperError("forced verification failure")

    monkeypatch.setattr(wrappers, "readback_wrappers", fail_readback)
    with pytest.raises(wrappers.WrapperError):
        wrappers.publish_wrappers("release-a", **layout_dirs)
    assert list(layout_dirs["managed_root"].iterdir()) == []
    assert list(layout_dirs["state_root"].iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("exists", "false", "existence"),
        ("mode", "0600", "mode"),
        ("mode", True, "mode"),
        ("content", 7, "content"),
        ("content", "YQ==", "content"),
    ),
)
def test_restore_rejects_coerced_snapshot_mapping_values(
    layout_dirs: dict[str, Path],
    field: str,
    value: object,
    message: str,
) -> None:
    snapshot = wrappers.capture_wrapper_snapshot(**layout_dirs)
    path = next(iter(snapshot))
    malformed = dict(snapshot)
    malformed[path] = {"exists": False, "content": b"", "mode": 0o600, field: value}

    with pytest.raises(wrappers.WrapperError, match=message):
        wrappers.restore_wrappers(
            malformed,
            _parent_guards=snapshot.parent_guards,
            **layout_dirs,
        )


def test_restore_rejects_incomplete_or_extended_snapshot_mapping(
    layout_dirs: dict[str, Path],
) -> None:
    snapshot = wrappers.capture_wrapper_snapshot(**layout_dirs)
    path = next(iter(snapshot))
    for item in (
        {"exists": False, "content": b""},
        {"exists": False, "content": b"", "mode": 0o600, "extra": True},
    ):
        malformed = dict(snapshot)
        malformed[path] = item
        with pytest.raises(wrappers.WrapperError, match="malformed snapshot"):
            wrappers.restore_wrappers(
                malformed,
                _parent_guards=snapshot.parent_guards,
                **layout_dirs,
            )


def test_restore_performs_readback_and_rolls_back_on_readback_failure(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(layout_dirs, "release-a")
    snapshot = wrappers.capture_wrapper_snapshot(**layout_dirs)
    _published(layout_dirs, "release-b")
    manifest = layout_dirs["managed_root"] / wrappers.WRAPPER_MANIFEST_NAME
    before = manifest.read_bytes()

    def fail_readback(**_kwargs: object) -> dict[str, object]:
        raise wrappers.WrapperError("forced restore readback failure")

    monkeypatch.setattr(wrappers, "readback_wrappers", fail_readback)
    with pytest.raises(wrappers.WrapperError, match="forced restore readback failure"):
        wrappers.restore_wrappers(snapshot, **layout_dirs)

    assert manifest.read_bytes() == before


def test_publish_parent_swap_never_writes_replacement_directory(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename after staging must stay on the anchored old directory FD."""

    managed = layout_dirs["managed_root"]
    outside = managed.with_name("outside-managed")
    outside.mkdir()
    detached = managed.with_name("managed-detached")
    original_renameat2 = release_manager._renameat2
    swapped = False

    def swap_then_renameat2(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            managed.rename(detached)
            managed.symlink_to(outside, target_is_directory=True)
        original_renameat2(
            descriptor,
            source,
            destination,
            flags=flags,
            label=label,
        )

    monkeypatch.setattr(release_manager, "_renameat2", swap_then_renameat2)
    with pytest.raises(wrappers.WrapperError, match="parent"):
        _published(layout_dirs)

    assert swapped
    assert list(outside.iterdir()) == []


def test_publish_absent_leaf_swap_never_overwrites_foreign_leaf(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intervening foreign destination wins over an absent publication."""

    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-absent-publish"
    outside.write_bytes(b"foreign absent publication leaf")
    outside.chmod(wrappers.WRAPPER_MODE)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and destination == target.name
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(wrappers.WrapperError, match="changed before commit"):
        _published(layout_dirs)

    assert injected
    assert target.read_bytes() == b"foreign absent publication leaf"
    assert not outside.exists()


def test_publish_replacement_leaf_swap_restores_foreign_leaf(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale A-to-B tombstone move restores a foreign leaf into place."""

    _published(layout_dirs, "release-a")
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-replacement-publish"
    outside.write_bytes(b"foreign replacement leaf")
    outside.chmod(wrappers.WRAPPER_MODE)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
                and flags == release_manager._RENAME_NOREPLACE
                and source == target.name
                and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(wrappers.WrapperError, match="changed before removal"):
        _published(layout_dirs, "release-b")

    assert injected
    assert target.read_bytes() == b"foreign replacement leaf"
    assert not outside.exists()


def test_publish_replacement_rejects_same_bytes_foreign_leaf_identity(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrapper preimages retain inode proof instead of comparing only bytes."""

    _published(layout_dirs, "release-a")
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-same-wrapper-bytes"
    outside.write_bytes(target.read_bytes())
    outside.chmod(stat.S_IMODE(target.stat().st_mode))
    outside_inode = outside.stat().st_ino
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_same_bytes_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_same_bytes_leaf)
    with pytest.raises(wrappers.WrapperError, match="changed before removal"):
        _published(layout_dirs, "release-b")

    assert injected
    assert target.stat().st_ino == outside_inode
    assert target.read_bytes().startswith(b"#!/usr/bin/python3\n")
    assert not outside.exists()


def test_absent_restore_leaf_swap_restores_foreign_leaf(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent-file rollback never unlinks a foreign leaf swapped after check."""

    empty = wrappers.capture_wrapper_snapshot(**layout_dirs)
    _published(layout_dirs, "release-a")
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-absent-restore"
    outside.write_bytes(b"foreign removal leaf")
    outside.chmod(wrappers.WRAPPER_MODE)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(wrappers.WrapperError, match="changed before removal"):
        wrappers.restore_wrappers(empty, **layout_dirs)

    assert injected
    assert target.read_bytes() == b"foreign removal leaf"
    assert not outside.exists()


def test_restore_replacement_leaf_swap_restores_foreign_leaf(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper rollback tombstones a foreign leaf without replacing it."""

    _published(layout_dirs, "release-a")
    snapshot_a = wrappers.capture_wrapper_snapshot(**layout_dirs)
    _published(layout_dirs, "release-b")
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-rollback-replacement"
    outside.write_bytes(b"foreign rollback leaf")
    outside.chmod(wrappers.WRAPPER_MODE)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
                and flags == release_manager._RENAME_NOREPLACE
                and source == target.name
                and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(wrappers.WrapperError, match="changed before removal"):
        wrappers.restore_wrappers(snapshot_a, **layout_dirs)

    assert injected
    assert target.read_bytes() == b"foreign rollback leaf"
    assert not outside.exists()


def test_transaction_rollback_tombstone_restores_original_preimage(
    layout_dirs: dict[str, Path],
) -> None:
    """A failed A-to-B transaction restores A through private tombstones."""

    target = layout_dirs["managed_root"] / "rollback-leaf"
    target.write_bytes(b"A")
    target.chmod(0o600)

    def fail_verification() -> None:
        raise RuntimeError("forced transaction verification failure")

    with pytest.raises(release_manager.ReleaseError, match="rolled back"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "rollback leaf"),),
            verify=fail_verification,
        )

    assert target.read_bytes() == b"A"
    assert [entry.name for entry in target.parent.iterdir()] == [target.name]


def test_transaction_rollback_tombstone_preserves_foreign_leaf_swap(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap during rollback cannot make recovery replace a foreign leaf."""

    target = layout_dirs["managed_root"] / "rollback-foreign-leaf"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = target.parent.parent / "outside-rollback-tombstone"
    outside.write_bytes(b"foreign leaf during rollback")
    outside.chmod(0o600)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_foreign_leaf(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
            and label == "rollback rollback leaf removal"
        ):
            injected = True
            os.replace(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_foreign_leaf)
    with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "rollback leaf"),),
            verify=lambda: (_ for _ in ()).throw(RuntimeError("forced verification failure")),
        )

    assert injected
    assert target.read_bytes() == b"foreign leaf during rollback"
    assert not outside.exists()


def test_transaction_rollback_tombstone_preserves_foreign_hardlink_swap(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback does not unlink a hard-linked external leaf swapped into B."""

    target = layout_dirs["managed_root"] / "rollback-hardlink-leaf"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = target.parent.parent / "outside-rollback-hardlink"
    outside.write_bytes(b"foreign hardlink during rollback")
    outside.chmod(0o600)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_hardlink(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
            and label == "rollback rollback hardlink leaf removal"
        ):
            injected = True
            target.unlink()
            os.link(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_hardlink)
    with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "rollback hardlink leaf"),),
            verify=lambda: (_ for _ in ()).throw(RuntimeError("forced verification failure")),
        )

    assert injected
    assert target.stat().st_ino == outside.stat().st_ino
    assert target.read_bytes() == b"foreign hardlink during rollback"
    assert outside.stat().st_nlink == 2


def test_transaction_rollback_parent_swap_never_touches_replacement_directory(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback uses its retained parent FD after a named-parent swap."""

    managed = layout_dirs["managed_root"]
    target = managed / "rollback-parent-leaf"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = managed.with_name("outside-rollback-parent")
    outside.mkdir()
    sentinel = outside / target.name
    sentinel.write_bytes(b"outside rollback sentinel")
    detached = managed.with_name("managed-detached-during-rollback")
    original_renameat2 = release_manager._renameat2
    swapped = False

    def swap_parent(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal swapped
        if (
            not swapped
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and label == "rollback rollback parent leaf removal"
        ):
            swapped = True
            managed.rename(detached)
            managed.symlink_to(outside, target_is_directory=True)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", swap_parent)
    with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "rollback parent leaf"),),
            verify=lambda: (_ for _ in ()).throw(RuntimeError("forced verification failure")),
        )

    assert swapped
    assert sentinel.read_bytes() == b"outside rollback sentinel"


def test_absent_restore_hardlink_swap_preserves_external_file(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-linked leaf injected at removal is restored, never unlinked."""

    empty = wrappers.capture_wrapper_snapshot(**layout_dirs)
    _published(layout_dirs, "release-a")
    target = layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]
    outside = target.parent.parent / "outside-hardlink-removal"
    outside.write_bytes(b"foreign hardlink removal leaf")
    outside.chmod(wrappers.WRAPPER_MODE)
    original_renameat2 = release_manager._renameat2
    injected = False

    def inject_hardlink(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        if (
            not injected
            and flags == release_manager._RENAME_NOREPLACE
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            target.unlink()
            os.link(outside, target)
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    monkeypatch.setattr(release_manager, "_renameat2", inject_hardlink)
    with pytest.raises(wrappers.WrapperError, match="changed before removal"):
        wrappers.restore_wrappers(empty, **layout_dirs)

    assert injected
    assert target.stat().st_ino == outside.stat().st_ino
    assert outside.read_bytes() == b"foreign hardlink removal leaf"
    assert outside.stat().st_nlink == 2


def test_publish_rejects_hardlinked_destination_without_touching_external_file(
    layout_dirs: dict[str, Path],
) -> None:
    managed = layout_dirs["managed_root"]
    outside = managed.with_name("outside-wrapper")
    outside.write_bytes(b"outside bytes")
    target = managed / wrappers.WRAPPER_NAMES[0]
    os.link(outside, target)

    with pytest.raises(wrappers.WrapperError, match="hard links"):
        _published(layout_dirs)

    assert outside.read_bytes() == b"outside bytes"
    assert target.stat().st_ino == outside.stat().st_ino


def test_publish_rejects_a_hardlinked_staging_file_before_rename(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final nlink check covers a staging file modified after fsync."""

    outside = layout_dirs["managed_root"].with_name("outside-stage")
    original_stage = release_manager._stage_publication_file
    linked = False

    def stage_then_link(*args: object, **kwargs: object) -> object:
        nonlocal linked
        staged = original_stage(*args, **kwargs)
        if not linked:
            linked = True
            os.link(
                staged.temporary_name,
                outside,
                src_dir_fd=staged.parent.descriptor,
                follow_symlinks=False,
            )
        return staged

    monkeypatch.setattr(release_manager, "_stage_publication_file", stage_then_link)
    with pytest.raises(wrappers.WrapperError, match="hard links"):
        _published(layout_dirs)

    assert linked
    assert not (layout_dirs["managed_root"] / wrappers.WRAPPER_NAMES[0]).exists()
    assert outside.stat().st_nlink == 2


def test_absent_restore_parent_swap_never_unlinks_replacement_directory(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an absent preimage is also descriptor-anchored and guarded."""

    empty = wrappers.capture_wrapper_snapshot(**layout_dirs)
    _published(layout_dirs)
    managed = layout_dirs["managed_root"]
    outside = managed.with_name("outside-restore")
    outside.mkdir()
    sentinel = outside / wrappers.WRAPPER_NAMES[0]
    sentinel.write_bytes(b"keep me")
    detached = managed.with_name("managed-before-restore")
    original_unlink = release_manager.os.unlink
    swapped = False

    def swap_then_unlink(name: str, *args: object, **kwargs: object) -> None:
        nonlocal swapped
        if not swapped and kwargs.get("dir_fd") is not None:
            swapped = True
            managed.rename(detached)
            managed.symlink_to(outside, target_is_directory=True)
        original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(release_manager.os, "unlink", swap_then_unlink)
    with pytest.raises(wrappers.WrapperError, match="parent"):
        wrappers.restore_wrappers(empty, **layout_dirs)

    assert swapped
    assert sentinel.read_bytes() == b"keep me"


def test_restart_recovery_cleans_a_durable_staged_phase(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after staging removes only the registered private temporary."""

    target = layout_dirs["managed_root"] / "journal-staged"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-staged.json"
    original_commit = release_manager._commit_staged_file
    original_restore = release_manager._restore_record

    def stop_before_commit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss before commit")

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_commit_staged_file", stop_before_commit)
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal staged"),),
                journal_path=journal,
            )
    assert target.read_bytes() == b"A"
    assert journal.exists()
    assert any(".journal-staged.tmp-" in path.name for path in target.parent.iterdir())

    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]
    assert release_manager._commit_staged_file is original_commit
    assert release_manager._restore_record is original_restore


def test_restart_recovery_restores_a_durable_quarantined_phase(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after A-to-quarantine restores A through its registered name."""

    target = layout_dirs["managed_root"] / "journal-quarantined"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-quarantined.json"
    original_renameat2 = release_manager._renameat2

    def interrupt_before_b(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        if source.startswith(f".{target.name}.tmp-") and destination == target.name:
            raise KeyboardInterrupt("simulate process loss after A quarantine")
        original_renameat2(descriptor, source, destination, flags=flags, label=label)

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_renameat2", interrupt_before_b)
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(KeyboardInterrupt, match="A quarantine"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal quarantined"),),
                journal_path=journal,
            )
    assert not target.exists()
    assert journal.exists()
    assert any(".journal-quarantined.quarantine-" in path.name for path in target.parent.iterdir())

    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_recovery_callback_retains_journal_until_exact_postimage_is_handed_off(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outer receipt can adopt recovered A/B identities before cleanup."""

    target = layout_dirs["managed_root"] / "journal-recovery-handoff"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-recovery-handoff.json"
    original_restore = release_manager._restore_record

    def interrupt_local_restore(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt("simulate hard death before local restore")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_restore_record", interrupt_local_restore)
        with pytest.raises(KeyboardInterrupt, match="hard death"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal recovery handoff"),),
                verify=lambda: (_ for _ in ()).throw(RuntimeError("force inverse")),
                journal_path=journal,
            )
    assert journal.exists()
    assert target.read_bytes() == b"B"

    handoffs: list[release_manager.PublicationTransactionReceipt] = []

    def reject_after_handoff(receipt: release_manager.PublicationTransactionReceipt) -> None:
        assert receipt.state == "recovered"
        assert journal.exists()
        handoffs.append(receipt)
        raise RuntimeError("outer durable checkpoint failed")

    with pytest.raises(RuntimeError, match="outer durable checkpoint failed"):
        release_manager.recover_publication_transaction(
            journal,
            after_recovery=reject_after_handoff,
        )
    assert target.read_bytes() == b"A"
    assert journal.exists()
    assert len(handoffs) == 1
    postimage = handoffs[0].postimages[target]
    assert postimage.identity is not None
    assert postimage.identity.inode == target.stat().st_ino

    completed = release_manager.recover_publication_transaction(journal)
    assert completed["state"] == "rolled-back"
    assert not journal.exists()


def test_keyboard_interrupt_after_a_quarantine_rolls_back_through_baseexception(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupts after an observed A move do not strand A or publish B."""

    target = layout_dirs["managed_root"] / "journal-keyboard-interrupt"
    target.write_bytes(b"A")
    target.chmod(0o600)
    original_renameat2 = release_manager._renameat2
    interrupted = False

    def interrupt_after_a_move(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal interrupted
        original_renameat2(descriptor, source, destination, flags=flags, label=label)
        if (
            not interrupted
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
        ):
            interrupted = True
            raise KeyboardInterrupt("interrupt after A quarantine")

    monkeypatch.setattr(release_manager, "_renameat2", interrupt_after_a_move)
    with pytest.raises(KeyboardInterrupt, match="after A quarantine"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal keyboard interrupt"),),
        )

    assert interrupted
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_keyboard_interrupt_after_b_publication_rolls_back_through_baseexception(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal after B's rename adopts its staged inode before rollback."""

    target = layout_dirs["managed_root"] / "journal-keyboard-after-b"
    target.write_bytes(b"A")
    target.chmod(0o600)
    original_capture = release_manager._capture_anchored_file
    interrupted = False

    def interrupt_first_b_capture(
        parent: release_manager._DirectoryAnchor,
        name: str,
        *,
        label: str,
    ) -> release_manager._AnchoredFileSnapshot:
        nonlocal interrupted
        if (
            not interrupted
            and name == target.name
            and target.exists()
            and target.read_bytes() == b"B"
        ):
            interrupted = True
            raise KeyboardInterrupt("interrupt after B publication")
        return original_capture(parent, name, label=label)

    monkeypatch.setattr(release_manager, "_capture_anchored_file", interrupt_first_b_capture)
    with pytest.raises(KeyboardInterrupt, match="after B publication"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal keyboard after B"),),
        )

    assert interrupted
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_keyboard_interrupt_after_absent_b_publication_removes_b(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-install/absent path has the same BaseException fence."""

    target = layout_dirs["managed_root"] / "journal-keyboard-absent-b"
    original_capture = release_manager._capture_anchored_file
    interrupted = False

    def interrupt_first_b_capture(
        parent: release_manager._DirectoryAnchor,
        name: str,
        *,
        label: str,
    ) -> release_manager._AnchoredFileSnapshot:
        nonlocal interrupted
        if (
            not interrupted
            and name == target.name
            and target.exists()
            and target.read_bytes() == b"B"
        ):
            interrupted = True
            raise KeyboardInterrupt("interrupt after absent B publication")
        return original_capture(parent, name, label=label)

    monkeypatch.setattr(release_manager, "_capture_anchored_file", interrupt_first_b_capture)
    with pytest.raises(KeyboardInterrupt, match="after absent B publication"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal keyboard absent B"),),
        )

    assert interrupted
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_postrename_absent_b_hardlink_retains_recovery_journal(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hardlink added before B's first capture never becomes a rolled-back B."""

    target = layout_dirs["managed_root"] / "journal-absent-b-hardlink"
    outside = target.parent.parent / "journal-absent-b-hardlink-outside"
    journal = target.parent / ".termdeck-publication-absent-b-hardlink.json"
    original_capture = release_manager._capture_anchored_file
    linked = False

    def link_before_first_postrename_capture(
        parent: release_manager._DirectoryAnchor,
        name: str,
        *,
        label: str,
    ) -> release_manager._AnchoredFileSnapshot:
        nonlocal linked
        if (
            not linked
            and name == target.name
            and target.exists()
            and target.read_bytes() == b"B"
        ):
            linked = True
            os.link(target, outside)
        return original_capture(parent, name, label=label)

    monkeypatch.setattr(
        release_manager,
        "_capture_anchored_file",
        link_before_first_postrename_capture,
    )
    with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal absent B hardlink"),),
            journal_path=journal,
        )

    assert linked
    assert target.read_bytes() == b"B"
    assert outside.read_bytes() == b"B"
    assert target.stat().st_nlink == outside.stat().st_nlink == 2
    assert journal.exists()
    with pytest.raises(release_manager.ReleaseError, match="restart recovery is incomplete"):
        release_manager.recover_publication_transaction(journal)
    assert target.exists()
    assert outside.exists()
    assert journal.exists()


def test_postrename_replacement_b_hardlink_retains_recovery_journal(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement path has the same no-terminal-claim fence."""

    target = layout_dirs["managed_root"] / "journal-replacement-b-hardlink"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = target.parent.parent / "journal-replacement-b-hardlink-outside"
    journal = target.parent / ".termdeck-publication-replacement-b-hardlink.json"
    original_capture = release_manager._capture_anchored_file
    linked = False

    def link_before_first_postrename_capture(
        parent: release_manager._DirectoryAnchor,
        name: str,
        *,
        label: str,
    ) -> release_manager._AnchoredFileSnapshot:
        nonlocal linked
        if (
            not linked
            and name == target.name
            and target.exists()
            and target.read_bytes() == b"B"
        ):
            linked = True
            os.link(target, outside)
        return original_capture(parent, name, label=label)

    monkeypatch.setattr(
        release_manager,
        "_capture_anchored_file",
        link_before_first_postrename_capture,
    )
    with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal replacement B hardlink"),),
            journal_path=journal,
        )

    assert linked
    assert target.read_bytes() == b"B"
    assert outside.read_bytes() == b"B"
    assert target.stat().st_nlink == outside.stat().st_nlink == 2
    assert journal.exists()
    with pytest.raises(release_manager.ReleaseError, match="restart recovery is incomplete"):
        release_manager.recover_publication_transaction(journal)
    assert target.exists()
    assert outside.exists()
    assert journal.exists()


def test_stage_intent_crash_retains_unregistered_private_leaf_for_recovery(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal between stage creation and identity journaling cannot orphan it."""

    target = layout_dirs["managed_root"] / "journal-stage-intent"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-stage-intent.json"
    original_stage = release_manager._stage_publication_file

    def stage_then_interrupt(*args: object, **kwargs: object) -> object:
        # Model process death after the file itself is durable but before its
        # full identity frame reaches the journal.  The native helper closes
        # ordinary Python signal windows by receiving these bindings itself.
        kwargs.pop("record", None)
        kwargs.pop("journal", None)
        staged = original_stage(*args, **kwargs)
        staged.close()
        raise KeyboardInterrupt("interrupt after stage creation")

    monkeypatch.setattr(release_manager, "_stage_publication_file", stage_then_interrupt)
    with pytest.raises(KeyboardInterrupt, match="after stage creation"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal stage intent"),),
            journal_path=journal,
        )

    staged = [path for path in target.parent.iterdir() if ".journal-stage-intent.tmp-" in path.name]
    assert target.read_bytes() == b"A"
    assert len(staged) == 1
    assert journal.exists()
    with pytest.raises(release_manager.ReleaseError, match="without a durable identity"):
        release_manager.recover_publication_transaction(journal)
    assert staged[0].exists()
    assert journal.exists()


def test_restart_recovery_restores_a_durable_published_phase(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after B publication proves B then restores the A preimage."""

    target = layout_dirs["managed_root"] / "journal-published"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-published.json"

    def fail_verification() -> None:
        raise RuntimeError("force rollback after B")

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal published"),),
                verify=fail_verification,
                journal_path=journal,
            )
    assert target.read_bytes() == b"B"
    assert journal.exists()

    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_restart_recovery_removes_a_durable_first_install_postimage(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published B is removed only when its staged identity remains exact."""

    target = layout_dirs["managed_root"] / "journal-first-install-published"
    journal = target.parent / ".termdeck-publication-first-install-published.json"

    def fail_verification() -> None:
        raise RuntimeError("force first-install rollback after B")

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during first-install rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal first-install published"),),
                verify=fail_verification,
                journal_path=journal,
            )
    assert target.read_bytes() == b"B"
    assert journal.exists()

    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_restart_recovery_finishes_an_interrupted_rollback_discard(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable B tombstone is cleaned only after the exact A is restored."""

    target = layout_dirs["managed_root"] / "journal-rollback-discard"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-rollback-discard.json"
    original_restore = release_manager._restore_quarantined_leaf
    interrupted = False

    def interrupt_before_a_restore(*args: object, **kwargs: object) -> None:
        nonlocal interrupted
        if kwargs.get("label") == "rollback journal rollback discard":
            interrupted = True
            raise KeyboardInterrupt("interrupt after B rollback tombstone")
        original_restore(*args, **kwargs)

    def fail_verification() -> None:
        raise RuntimeError("force rollback after B")

    monkeypatch.setattr(
        release_manager,
        "_restore_quarantined_leaf",
        interrupt_before_a_restore,
    )
    with pytest.raises(KeyboardInterrupt, match="after B rollback tombstone"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal rollback discard"),),
            verify=fail_verification,
            journal_path=journal,
        )

    assert interrupted
    assert not target.exists()
    assert journal.exists()
    assert sum("quarantine-" in path.name for path in target.parent.iterdir()) == 2

    monkeypatch.setattr(release_manager, "_restore_quarantined_leaf", original_restore)
    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_restart_recovery_adopts_b_tombstone_before_local_assignment(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal after B's tombstone rename still leaves an exact recovery path."""

    target = layout_dirs["managed_root"] / "journal-rollback-preassignment"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-rollback-preassignment.json"
    original_unlink = release_manager._commit_unlink
    interrupted = False

    def interrupt_after_b_tombstone(
        parents: release_manager._PublicationParents,
        record: release_manager._PublicationRecord,
        **kwargs: object,
    ) -> None:
        nonlocal interrupted
        original_unlink(parents, record, **kwargs)
        if record.label == "rollback journal rollback preassignment":
            interrupted = True
            raise KeyboardInterrupt("interrupt before rollback tombstone assignment")

    def fail_verification() -> None:
        raise RuntimeError("force rollback after B")

    monkeypatch.setattr(release_manager, "_commit_unlink", interrupt_after_b_tombstone)
    with pytest.raises(KeyboardInterrupt, match="before rollback tombstone assignment"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal rollback preassignment"),),
            verify=fail_verification,
            journal_path=journal,
        )

    assert interrupted
    assert not target.exists()
    assert journal.exists()

    monkeypatch.setattr(release_manager, "_commit_unlink", original_unlink)
    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_restart_recovery_finishes_after_a_restore_before_phase_ack(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback A-restore rename is itself durably resumable."""

    target = layout_dirs["managed_root"] / "journal-rollback-restored"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-rollback-restored.json"
    original_restore = release_manager._restore_quarantined_leaf
    interrupted = False

    def interrupt_after_a_restore(*args: object, **kwargs: object) -> None:
        nonlocal interrupted
        original_restore(*args, **kwargs)
        if kwargs.get("label") == "rollback journal rollback restored":
            interrupted = True
            raise KeyboardInterrupt("interrupt after A rollback restore")

    def fail_verification() -> None:
        raise RuntimeError("force rollback after B")

    monkeypatch.setattr(
        release_manager,
        "_restore_quarantined_leaf",
        interrupt_after_a_restore,
    )
    with pytest.raises(KeyboardInterrupt, match="after A rollback restore"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal rollback restored"),),
            verify=fail_verification,
            journal_path=journal,
        )

    assert interrupted
    assert target.read_bytes() == b"A"
    assert journal.exists()
    monkeypatch.setattr(release_manager, "_restore_quarantined_leaf", original_restore)
    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_restart_recovery_rejects_a_same_bytes_foreign_b_identity(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery binds B to the staged inode, rather than its content alone."""

    target = layout_dirs["managed_root"] / "journal-same-bytes-b"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = target.parent.parent / "journal-same-bytes-b-outside"
    outside.write_bytes(b"B")
    outside.chmod(0o600)
    outside_inode = outside.stat().st_ino
    journal = target.parent / ".termdeck-publication-same-bytes-b.json"

    def fail_verification() -> None:
        raise RuntimeError("force rollback after B")

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(release_manager.ReleaseError, match="rollback was incomplete"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal same bytes B"),),
                verify=fail_verification,
                journal_path=journal,
            )
    outside.replace(target)

    with pytest.raises(release_manager.ReleaseError, match="foreign canonical"):
        release_manager.recover_publication_transaction(journal)

    assert target.read_bytes() == b"B"
    assert target.stat().st_ino == outside_inode
    assert journal.exists()


def test_quarantine_reoccupation_is_retryable_without_touching_foreign_leaf(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign canonical leaf survives both failed and later successful recovery."""

    target = layout_dirs["managed_root"] / "journal-foreign"
    target.write_bytes(b"A")
    target.chmod(0o600)
    outside = target.parent.parent / "journal-foreign-outside"
    outside.write_bytes(b"foreign")
    outside.chmod(0o600)
    journal = target.parent / ".termdeck-publication-foreign.json"
    original_renameat2 = release_manager._renameat2
    injected = False

    def occupy_after_a_quarantine(
        descriptor: int,
        source: str,
        destination: str,
        *,
        flags: int,
        label: str,
    ) -> None:
        nonlocal injected
        original_renameat2(descriptor, source, destination, flags=flags, label=label)
        if (
            not injected
            and source == target.name
            and destination.startswith(f".{target.name}.quarantine-")
        ):
            injected = True
            outside.replace(target)
            raise KeyboardInterrupt("simulate abrupt reoccupation")

    def stop_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulate process loss during rollback")

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_renameat2", occupy_after_a_quarantine)
        scoped.setattr(release_manager, "_restore_record", stop_rollback)
        with pytest.raises(KeyboardInterrupt, match="reoccupation"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal foreign"),),
                journal_path=journal,
            )
    assert injected
    assert target.read_bytes() == b"foreign"
    with pytest.raises(release_manager.ReleaseError, match="foreign canonical"):
        release_manager.recover_publication_transaction(journal)
    assert target.read_bytes() == b"foreign"
    assert journal.exists()

    target.unlink()
    recovered = release_manager.recover_publication_transaction(journal)

    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert list(target.parent.iterdir()) == [target]


def test_committed_cleanup_fsync_failure_keeps_b_without_claiming_a_rollback(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-unlink fsync failure is deferred cleanup after durable B commit."""

    target = layout_dirs["managed_root"] / "journal-committed-cleanup"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-committed-cleanup.json"
    original_unlink = release_manager._unlinkat
    original_fsync = release_manager._DirectoryAnchor.fsync
    removed_quarantine = False

    def unlink_then_mark(descriptor: int, name: str, *, label: str) -> None:
        nonlocal removed_quarantine
        original_unlink(descriptor, name, label=label)
        if "quarantine-" in name and ".dispose-" in name:
            removed_quarantine = True

    def fail_only_post_unlink(
        anchor: release_manager._DirectoryAnchor, *, label: str
    ) -> None:
        if removed_quarantine:
            raise release_manager.ReleaseError("injected post-unlink directory fsync failure")
        original_fsync(anchor, label=label)

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager, "_unlinkat", unlink_then_mark)
        scoped.setattr(release_manager._DirectoryAnchor, "fsync", fail_only_post_unlink)
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal committed cleanup"),),
            journal_path=journal,
        )

    assert removed_quarantine
    assert target.read_bytes() == b"B"
    assert not any("quarantine-" in path.name for path in target.parent.iterdir())
    # B's committed frame survived before A was discarded.  The journal is
    # retained for its missing disposal acknowledgement, not misclassified as
    # a rollback which would require the already-unlinked A inode.
    assert journal.exists()
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "committed-cleaned"
    assert target.read_bytes() == b"B"
    assert not journal.exists()


def test_uncertain_committed_frame_retains_journal_for_restart_decision(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed terminal-frame write cannot be treated as an in-memory commit."""

    target = layout_dirs["managed_root"] / "journal-uncertain-commit"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-uncertain-commit.json"
    original_persist = release_manager._PublicationJournal.persist
    original_write = release_manager._write_all
    terminal_persist = False

    def mark_terminal_persist(
        value: release_manager._PublicationJournal,
        *,
        state: str | None = None,
    ) -> None:
        nonlocal terminal_persist
        prior = terminal_persist
        terminal_persist = state == "committed"
        try:
            original_persist(value, state=state)
        finally:
            terminal_persist = prior

    def fail_terminal_journal_write(
        descriptor: int, content: bytes, *, label: str
    ) -> None:
        if terminal_persist and label == "publication journal":
            raise release_manager.ReleaseError("injected terminal journal write failure")
        original_write(descriptor, content, label=label)

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager._PublicationJournal, "persist", mark_terminal_persist)
        scoped.setattr(release_manager, "_write_all", fail_terminal_journal_write)
        with pytest.raises(release_manager.ReleaseError, match="durability is uncertain"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal uncertain commit"),),
                journal_path=journal,
            )

    # The public rename did happen, but the state bit was never inferred from
    # the failed fsync.  The durable active frame still has A's quarantine and
    # is the only authority allowed to choose the inverse after restart.
    assert target.read_bytes() == b"B"
    assert journal.exists()
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert not journal.exists()


@pytest.mark.parametrize(
    "generations",
    (
        (1, 1),
        (2, 1),
    ),
)
def test_journal_reader_rejects_ambiguous_complete_frame_generations(
    generations: tuple[int, int],
) -> None:
    """A possibly persisted append can never be retried with the same sequence."""

    frames = b"".join(
        release_manager._publication_journal_frame({"generation": generation})
        for generation in generations
    )

    with pytest.raises(release_manager.ReleaseError, match="ambiguous frame generation"):
        release_manager._publication_journal_latest_payload(frames)


def test_journal_create_parent_fsync_failure_retains_recovery_handle(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mutation is labelled rolled back when its initial journal is unsettled."""

    target = layout_dirs["managed_root"] / "journal-create-fsync"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-create-fsync.json"
    original_fsync = release_manager._DirectoryAnchor.fsync

    def fail_initial_parent_fsync(
        anchor: release_manager._DirectoryAnchor, *, label: str
    ) -> None:
        if label == "publication journal parent":
            raise release_manager.ReleaseError("injected initial journal parent fsync failure")
        original_fsync(anchor, label=label)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            release_manager._DirectoryAnchor,
            "fsync",
            fail_initial_parent_fsync,
        )
        with pytest.raises(release_manager.ReleaseError, match="recovery handle retained"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal create fsync"),),
                journal_path=journal,
            )

    assert target.read_bytes() == b"A"
    assert journal.exists()
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert not journal.exists()


@pytest.mark.parametrize("fault", ("fchmod", "fstat", "validate"))
def test_pre_result_journal_failure_never_leaves_a_zero_byte_orphan(
    layout_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """Setup failures either remove the exact O_EXCL leaf or retain a receipt.

    The journal is created before any staging or canonical rename.  A failure
    in that narrow interval used to close its descriptor and leave a zero-byte
    fixed journal which a restart could only reject as "no complete durable
    record".  Each hook fails once so the authenticated cleanup/retry path is
    observable without weakening its later identity checks.
    """

    target = layout_dirs["managed_root"] / f"journal-pre-result-{fault}"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / f".termdeck-publication-pre-result-{fault}.json"
    failed = False

    if fault == "fchmod":
        original_fchmod = release_manager.os.fchmod

        def fail_once(descriptor: int, mode: int) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EIO, "injected journal fchmod failure")
            original_fchmod(descriptor, mode)

        monkeypatch.setattr(release_manager.os, "fchmod", fail_once)
    elif fault == "fstat":
        original_identity = release_manager._file_identity

        def fail_once(info: os.stat_result, *, label: str) -> release_manager._FileIdentity:
            nonlocal failed
            if label == "publication journal" and not failed:
                failed = True
                raise release_manager.ReleaseError("injected journal fstat failure")
            return original_identity(info, label=label)

        monkeypatch.setattr(release_manager, "_file_identity", fail_once)
    else:
        original_validate = release_manager._validate_regular_at

        def fail_once(
            parent: release_manager._DirectoryAnchor,
            name: str,
            *,
            label: str,
        ) -> release_manager._FileIdentity | None:
            nonlocal failed
            if label == "publication journal" and not failed:
                failed = True
                raise release_manager.ReleaseError("injected journal validate failure")
            return original_validate(parent, name, label=label)

        monkeypatch.setattr(release_manager, "_validate_regular_at", fail_once)

    with pytest.raises(release_manager.ReleaseError, match="recovery handle retained"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, f"journal pre-result {fault}"),),
            journal_path=journal,
        )

    assert failed
    assert target.read_bytes() == b"A"
    # Once setup has failed, the parseable active frame is deliberately kept
    # at its stable name.  A local unlink/fsync ambiguity cannot turn that
    # only recovery handle into an undiscoverable disposal leaf.
    assert journal.exists()
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert not journal.exists()


def test_pre_result_journal_failure_parent_fsync_retains_parseable_receipt(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncertain cleanup never downgrades to an unreadable journal orphan."""

    target = layout_dirs["managed_root"] / "journal-pre-result-parent-fsync"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-pre-result-parent-fsync.json"
    original_fchmod = release_manager.os.fchmod
    original_fsync = release_manager._DirectoryAnchor.fsync
    fchmod_failed = False
    receipt_parent_failed = False

    def fail_first_fchmod(descriptor: int, mode: int) -> None:
        nonlocal fchmod_failed
        if not fchmod_failed:
            fchmod_failed = True
            raise OSError(errno.EIO, "injected journal fchmod failure")
        original_fchmod(descriptor, mode)

    def fail_failure_receipt_parent(
        anchor: release_manager._DirectoryAnchor, *, label: str
    ) -> None:
        nonlocal receipt_parent_failed
        if label == "publication journal failure receipt parent":
            receipt_parent_failed = True
            raise release_manager.ReleaseError("injected failure receipt parent fsync")
        original_fsync(anchor, label=label)

    with monkeypatch.context() as scoped:
        scoped.setattr(release_manager.os, "fchmod", fail_first_fchmod)
        scoped.setattr(
            release_manager._DirectoryAnchor,
            "fsync",
            fail_failure_receipt_parent,
        )
        with pytest.raises(release_manager.ReleaseError, match="recovery handle retained"):
            release_manager._publish_files_transactionally(
                ((target, b"B", 0o600, "journal pre-result parent fsync"),),
                journal_path=journal,
            )

    assert fchmod_failed and receipt_parent_failed
    assert target.read_bytes() == b"A"
    assert journal.exists()
    # The receipt frame itself was fsynced before its parent-fsync ambiguity,
    # so recovery can make the only valid decision without parsing a blank
    # O_EXCL file.
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "rolled-back"
    assert target.read_bytes() == b"A"
    assert not journal.exists()


def test_pre_result_journal_keyboard_interrupt_is_rethrown_with_recovery_receipt(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable failure recording must not convert cancellation into ReleaseError."""

    target = layout_dirs["managed_root"] / "journal-pre-result-interrupt"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-pre-result-interrupt.json"

    def interrupt_fchmod(_descriptor: int, _mode: int) -> None:
        raise KeyboardInterrupt("injected journal creation cancellation")

    monkeypatch.setattr(release_manager.os, "fchmod", interrupt_fchmod)
    with pytest.raises(KeyboardInterrupt, match="creation cancellation"):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal pre-result interrupt"),),
            journal_path=journal,
        )

    assert target.read_bytes() == b"A"
    assert journal.exists()
    recovered = release_manager.recover_publication_transaction(journal)
    assert recovered["state"] == "rolled-back"
    assert not journal.exists()


def test_pre_result_journal_foreign_replacement_is_never_unlinked(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement during fchmod failure remains the foreign journal winner."""

    target = layout_dirs["managed_root"] / "journal-pre-result-foreign"
    target.write_bytes(b"A")
    target.chmod(0o600)
    journal = target.parent / ".termdeck-publication-pre-result-foreign.json"
    outside = target.parent.parent / "foreign-journal-winner"
    outside.write_bytes(b"foreign journal winner")
    outside.chmod(0o600)
    original_fchmod = release_manager.os.fchmod
    swapped = False

    def replace_then_fail(descriptor: int, mode: int) -> None:
        nonlocal swapped
        original_fchmod(descriptor, mode)
        if not swapped:
            swapped = True
            outside.replace(journal)
            raise OSError(errno.EIO, "injected post-fchmod journal replacement")

    monkeypatch.setattr(release_manager.os, "fchmod", replace_then_fail)
    with pytest.raises(
        release_manager.ReleaseError,
        match="created leaf could not be authenticated for cleanup",
    ):
        release_manager._publish_files_transactionally(
            ((target, b"B", 0o600, "journal pre-result foreign"),),
            journal_path=journal,
        )

    assert swapped
    assert target.read_bytes() == b"A"
    assert journal.read_bytes() == b"foreign journal winner"
    assert not outside.exists()


def test_publication_primitives_retry_bounded_eintr(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename, unlink, and fsync retry an interrupted syscall instead of failing open."""

    target = layout_dirs["managed_root"] / "eintr-leaf"
    target.write_bytes(b"x")
    parent = release_manager._open_directory_anchor(
        target.parent,
        label="eintr parent",
        create_parents=False,
    )
    original_unlink = release_manager.os.unlink
    original_fsync = release_manager.os.fsync
    unlink_calls = 0
    fsync_calls = 0

    def interrupted_unlink(name: str, *args: object, **kwargs: object) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            raise OSError(errno.EINTR, "interrupted")
        original_unlink(name, *args, **kwargs)

    def interrupted_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError(errno.EINTR, "interrupted")
        original_fsync(descriptor)

    class FakeRename:
        def __init__(self) -> None:
            self.calls = 0
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *_args: object) -> int:
            self.calls += 1
            if self.calls == 1:
                release_manager.ctypes.set_errno(errno.EINTR)
                return -1
            return 0

    class FakeLib:
        def __init__(self) -> None:
            self.renameat2 = FakeRename()

    fake = FakeLib()
    try:
        monkeypatch.setattr(release_manager.os, "unlink", interrupted_unlink)
        monkeypatch.setattr(release_manager.os, "fsync", interrupted_fsync)
        release_manager._unlinkat(parent.descriptor, target.name, label="eintr leaf")
        parent.fsync(label="eintr parent")
        monkeypatch.setattr(release_manager.ctypes, "CDLL", lambda *_args, **_kwargs: fake)
        release_manager._renameat2(
            parent.descriptor,
            "source",
            "destination",
            flags=release_manager._RENAME_NOREPLACE,
            label="eintr rename",
        )
    finally:
        parent.close()

    assert unlink_calls == 2
    assert fsync_calls == 2
    assert fake.renameat2.calls == 2
    assert not target.exists()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_checkpoint_wait_interruption_cleans_before_propagating(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    original = error_type("checkpoint interrupted")

    class InterruptedProcess:
        def wait(self, *, timeout: float) -> int:
            raise original

    process = InterruptedProcess()
    cleaned: list[object] = []
    monkeypatch.setattr(wrappers.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        wrappers, "_wait_checkpoint_exit", lambda child, **kwargs: child.wait(**kwargs)
    )
    monkeypatch.setattr(
        wrappers, "_cleanup_checkpoint_process", lambda child: cleaned.append(child) or True
    )
    with pytest.raises(error_type) as caught:
        wrappers.stop_unit("termdeck-checkpoint.service", **layout_dirs)
    assert caught.value is original
    assert cleaned == [process]


def test_checkpoint_cleanup_interruption_preserves_original_wait_error(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = KeyboardInterrupt("original interrupt")

    class InterruptedProcess:
        def wait(self, *, timeout: float) -> int:
            raise original

    def interrupted_cleanup(_process: object) -> bool:
        raise SystemExit("second interruption during cleanup")

    monkeypatch.setattr(wrappers.subprocess, "Popen", lambda *_args, **_kwargs: InterruptedProcess())
    monkeypatch.setattr(
        wrappers, "_wait_checkpoint_exit", lambda child, **kwargs: child.wait(**kwargs)
    )
    monkeypatch.setattr(wrappers, "_cleanup_checkpoint_process", interrupted_cleanup)
    with pytest.raises(KeyboardInterrupt) as caught:
        wrappers.stop_unit("termdeck-checkpoint.service", **layout_dirs)
    assert caught.value is original


def test_checkpoint_missing_process_group_still_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    class ExitedProcess:
        pid = 4321
        returncode = None

        def wait(self, *, timeout: float) -> int:
            waits.append(timeout)
            return 0

    def missing_group(_pid: int, _sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(wrappers.os, "killpg", missing_group)
    assert wrappers._cleanup_checkpoint_process(ExitedProcess()) is True
    assert waits == [wrappers.CHECKPOINT_TERMINATE_GRACE]


def test_checkpoint_reaped_leader_never_signals_a_reusable_group_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4321, returncode=0)
    monkeypatch.setattr(wrappers, "_checkpoint_group_is_running", lambda _pid: True)
    monkeypatch.setattr(
        wrappers.os,
        "killpg",
        lambda *_args: pytest.fail("a reaped checkpoint PID no longer reserves its group ID"),
    )
    assert wrappers._cleanup_checkpoint_process(process) is False


def test_checkpoint_cleanup_escalates_after_interruption_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    waits: list[float] = []
    original = KeyboardInterrupt("interrupted while awaiting group exit")

    class Process:
        pid = 4321
        returncode = None

        def wait(self, *, timeout: float) -> int:
            waits.append(timeout)
            self.returncode = -int(signal.SIGKILL)
            return self.returncode

    def group_wait(_pid: int, *, timeout: float) -> bool:
        if len(signals) == 1:
            raise original
        return True

    monkeypatch.setattr(wrappers.os, "killpg", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(wrappers, "_wait_checkpoint_group", group_wait)
    with pytest.raises(KeyboardInterrupt) as caught:
        wrappers._cleanup_checkpoint_process(Process())
    assert caught.value is original
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert waits == [wrappers.CHECKPOINT_KILL_GRACE]


def test_checkpoint_group_cleanup_remains_bounded_and_reaps_on_incomplete_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    waits: list[float] = []
    group_waits: list[float] = []

    class Process:
        pid = 4321
        returncode = None

        def wait(self, *, timeout: float) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired("checkpoint", timeout)

    def group_wait(_pid: int, *, timeout: float) -> bool:
        group_waits.append(timeout)
        return False

    monkeypatch.setattr(wrappers.os, "killpg", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(wrappers, "_wait_checkpoint_group", group_wait)
    assert wrappers._cleanup_checkpoint_process(Process()) is False
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert group_waits == [wrappers.CHECKPOINT_TERMINATE_GRACE, wrappers.CHECKPOINT_KILL_GRACE]
    assert waits == [wrappers.CHECKPOINT_KILL_GRACE]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process-group regression")
@pytest.mark.parametrize("interrupt_after_exit", [False, True])
def test_checkpoint_kills_surviving_child_and_preserves_unrelated_process(
    tmp_path: Path, interrupt_after_exit: bool
) -> None:
    """Use a disposable subreaper so this real fork test leaves no orphans."""

    import sys
    import textwrap

    child_program = textwrap.dedent(
        """
        import os, signal, sys, time
        from pathlib import Path
        if os.fork() == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(sys.argv[1]).write_text(str(os.getpid()))
            while True:
                time.sleep(1)
        while not Path(sys.argv[1] + '.exit').exists():
            time.sleep(0.01)
        os._exit(0)
        """
    )
    supervisor = textwrap.dedent(
        """
        import ctypes
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from termdeck import service_wrappers as wrappers

        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
        wrappers.CHECKPOINT_TERMINATE_GRACE = 0.2
        wrappers.CHECKPOINT_KILL_GRACE = 1.0
        ready = Path(sys.argv[1])
        child_program = sys.argv[2]
        interrupt_after_exit = sys.argv[3] == 'True'
        leader = subprocess.Popen(
            [sys.executable, '-c', child_program, str(ready)], start_new_session=True
        )
        unrelated = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() or not ready.read_text():
                assert time.monotonic() < deadline, 'child did not become ready'
                time.sleep(0.01)
            child_pid = int(ready.read_text())
            started = time.monotonic()
            if interrupt_after_exit:
                original = KeyboardInterrupt('SIGINT after checkpoint leader exit')
                interrupted = False
                real_wait = leader.wait
                real_waitid = os.waitid

                def raise_interrupt(_sig, _frame):
                    raise original

                def waitid_then_interrupt(kind, pid, flags):
                    global interrupted
                    assert flags & os.WNOWAIT, 'checkpoint wait may reap its leader'
                    result = real_waitid(kind, pid, flags)
                    if result is not None:
                        assert leader.returncode is None, 'leader reaped before cancellation'
                        interrupted = True
                        os.kill(os.getpid(), signal.SIGINT)
                    return result

                def reap_after_cleanup(*args, **kwargs):
                    assert interrupted, 'Popen.wait called before cancellation cleanup'
                    return real_wait(*args, **kwargs)

                signal.signal(signal.SIGINT, raise_interrupt)
                leader.wait = reap_after_cleanup
                wrappers.os.waitid = waitid_then_interrupt
                wrappers.subprocess.Popen = lambda *_args, **_kwargs: leader
                wrappers.build_stop_command = lambda *_args, **_kwargs: leader.args
                layout = SimpleNamespace(
                    source_root=ready.parent, execution_root=ready.parent,
                    execution_binding=(), control_root=ready.parent,
                    runtime_root=ready.parent, state_root=ready.parent,
                    managed_root=ready.parent, managed_bin=ready.parent,
                    unit_dir=ready.parent,
                )
                Path(str(ready) + '.exit').touch()
                try:
                    wrappers._run_checkpoint(layout)
                except KeyboardInterrupt as caught:
                    assert caught is original, 'original SIGINT was replaced'
                else:
                    raise AssertionError('SIGINT did not propagate')
                finally:
                    leader.wait = real_wait
                    wrappers.os.waitid = real_waitid
            else:
                assert wrappers._cleanup_checkpoint_process(leader)
            assert time.monotonic() - started < 4, 'cleanup exceeded its budget'
            assert leader.returncode == (0 if interrupt_after_exit else -signal.SIGTERM)
            assert unrelated.poll() is None, 'cleanup signaled an unrelated process'
            deadline = time.monotonic() + 2
            while True:
                reaped, status = os.waitpid(child_pid, os.WNOHANG)
                if reaped:
                    child_pid = None
                    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
                    break
                assert time.monotonic() < deadline, 'checkpoint child survived cleanup'
                time.sleep(0.01)
        finally:
            if leader.returncode is None:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                leader.wait(timeout=2)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            unrelated.terminate()
            unrelated.wait(timeout=2)
        """
    )
    result = subprocess.run(
        [
            sys.executable, "-c", supervisor, str(tmp_path / "checkpoint-child.pid"),
            child_program, str(interrupt_after_exit),
        ],
        cwd=Path(wrappers.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("replacement_phase", ["stale-check", "exit-wait"])
def test_stop_preserves_concurrent_start_identity(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, replacement_phase: str
) -> None:
    _published(layout_dirs)
    path = layout_dirs["state_root"] / "termdeck-web-backend.service.conf"
    original = json.loads(path.read_bytes())
    original.update(
        pid=111, start_ticks=1, exe="/usr/bin/true", argv=["/usr/bin/true"],
        command_sha256="original", state="starting",
    )
    path.write_bytes(wrappers._canonical_json(original))
    replacement = dict(original, pid=222, start_ticks=2, command_sha256="replacement")
    replacement_bytes = wrappers._canonical_json(replacement)
    signals: list[tuple[int, int]] = []

    def replace_during_verification(_identity: object) -> bool:
        if replacement_phase == "stale-check":
            path.write_bytes(replacement_bytes)
            return False
        return True

    def replace_during_wait(_descriptor: int, *, timeout: float) -> bool:
        assert replacement_phase == "exit-wait"
        path.write_bytes(replacement_bytes)
        return True

    monkeypatch.setattr(wrappers, "verify_process_identity", replace_during_verification)
    monkeypatch.setattr(wrappers, "proc_start_ticks", lambda _pid: 9)
    monkeypatch.setattr(wrappers, "_validate_recorded_command", lambda *_args: None)
    pidfd = os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr(wrappers, "_open_identity_pidfd", lambda _pid, _ticks: pidfd)
    monkeypatch.setattr(
        wrappers,
        "_pidfd_send_signal",
        lambda _descriptor, sig: signals.append((111, sig)),
    )
    monkeypatch.setattr(wrappers, "_wait_for_pidfd_exit", replace_during_wait)
    with pytest.raises(wrappers.WrapperError, match="changed after preflight"):
        wrappers.stop_unit("termdeck-web-backend.service", release_id="release-a", **layout_dirs)
    assert path.read_bytes() == replacement_bytes
    assert signals == ([] if replacement_phase == "stale-check" else [(111, signal.SIGTERM)])


def test_stop_during_pre_exec_handoff_preserves_live_start_identity(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(layout_dirs)
    layout = wrappers.resolve_layout(**layout_dirs)
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-backend.service"]
    wrappers._record_start_identity(spec, layout, "release-a", ("/usr/bin/true",))
    path = layout.config_path(spec)
    before = path.read_bytes()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(wrappers.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(wrappers.WrapperError, match="live process identity changed"):
        wrappers.stop_unit(spec, release_id="release-a", **layout_dirs)
    assert path.read_bytes() == before
    assert signals == []


@pytest.mark.parametrize("replacement_phase", ["before-read", "before-write"])
def test_exec_failure_does_not_mark_an_intervening_start_failed(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, replacement_phase: str
) -> None:
    _published(layout_dirs)
    path = layout_dirs["state_root"] / "termdeck-web-backend.service.conf"
    replacement_bytes: list[bytes] = []

    def replace_start() -> None:
        replacement = json.loads(path.read_bytes())
        replacement.update(pid=os.getpid() + 1000, start_ticks=1)
        replacement_bytes.append(wrappers._canonical_json(replacement))
        path.write_bytes(replacement_bytes[0])

    def failed_exec(_program: str, _argv: list[str]) -> None:
        if replacement_phase == "before-read":
            replace_start()
        raise OSError(errno.ENOENT, "injected exec failure")

    original_write = wrappers._atomic_write_json

    def replace_before_failed_write(destination: Path, value: dict[str, object], **kwargs: object) -> None:
        if replacement_phase == "before-write" and value.get("state") == "failed":
            replace_start()
        original_write(destination, value, **kwargs)

    monkeypatch.setattr(wrappers, "_preflight_unit_start", lambda *_args: ("/usr/bin/true",))
    monkeypatch.setattr(wrappers.os, "execv", failed_exec)
    monkeypatch.setattr(wrappers, "_atomic_write_json", replace_before_failed_write)
    with pytest.raises(wrappers.WrapperError, match="could not exec|changed after preflight"):
        wrappers.run_wrapper("termdeck-web-backend.service", "start", release_id="release-a", **layout_dirs)
    assert path.read_bytes() == replacement_bytes[0]
    assert json.loads(path.read_bytes())["state"] == "starting"


@pytest.mark.parametrize("prior_binding", ["verified", "unverified", "wrong-root", "legacy"])
def test_recovery_stop_validates_carried_release_execution_root(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, prior_binding: str
) -> None:
    prior_root = layout_dirs["managed_root"] / "releases" / "release-a" / "site"
    current_root = layout_dirs["managed_root"] / "releases" / "release-b" / "site"
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    old_layout = wrappers.resolve_layout(**layout_dirs, execution_root=prior_root)
    new_layout = wrappers.resolve_layout(**layout_dirs, execution_root=current_root)
    old_command = wrappers.build_start_command(spec, **layout_dirs, execution_root=prior_root)
    old_argv = wrappers._expected_proc_argv(old_command)
    old_config = wrappers._initial_config(spec, old_layout, "release-a")
    old_config.update(
        pid=111, start_ticks=1, exe=wrappers._expected_proc_executable(old_command[0]),
        argv=list(old_argv), command_sha256=wrappers._sha256_bytes(wrappers._canonical_json(list(old_argv))),
        state="starting",
    )
    carried = json.loads(wrappers._carry_runtime_identity(
        spec, old_layout.config_path(spec),
        wrappers._canonical_json(wrappers._initial_config(spec, new_layout, "release-b")),
        existing_content=wrappers._canonical_json(old_config),
    ))
    verified: list[str] = []

    def verify_prior(manager_layout: object, release_id: str) -> dict[str, object]:
        assert manager_layout.root == layout_dirs["managed_root"]
        verified.append(release_id)
        if prior_binding == "unverified":
            raise release_manager.ReleaseError("prior release verification failed")
        return {
            "execution_root": current_root if prior_binding == "wrong-root" else prior_root,
            "execution_binding": {} if prior_binding == "legacy" else {"verified": True},
        }

    monkeypatch.setattr(release_manager, "_release_wrapper_execution_kwargs", verify_prior)
    if prior_binding == "verified":
        wrappers._validate_recorded_command(spec, new_layout, carried)
    else:
        with pytest.raises(wrappers.WrapperError, match="recorded recovery release"):
            wrappers._validate_recorded_command(spec, new_layout, carried)
    assert verified == ["release-a"]
    assert carried["release_id"] == "release-b"
    assert carried["argv"] == list(old_argv)


@pytest.mark.parametrize("mutation", ["foreign-root", "different-command"])
def test_recovery_stop_rejects_untrusted_carried_command(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    current_root = layout_dirs["managed_root"] / "releases" / "release-b" / "site"
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    layout = wrappers.resolve_layout(**layout_dirs, execution_root=current_root)
    command = wrappers.build_start_command(spec, **layout_dirs, execution_root=current_root)
    argv = list(wrappers._expected_proc_argv(command))
    argv[5] = str(layout_dirs["source_root"] if mutation == "foreign-root" else (
        layout_dirs["managed_root"] / "releases" / "release-a" / "site"
    ))
    if mutation == "different-command":
        argv[-1] = str(layout_dirs["source_root"])
    config = {
        "argv": argv, "command_sha256": wrappers._sha256_bytes(wrappers._canonical_json(argv)),
        "exe": wrappers._expected_proc_executable(command[0]),
    }

    def unexpected_verification(*_args: object) -> None:
        raise AssertionError("invalid command must be rejected before release verification")

    monkeypatch.setattr(release_manager, "_release_wrapper_execution_kwargs", unexpected_verification)
    with pytest.raises(wrappers.WrapperError, match="recorded recovery execution root|recorded argv"):
        wrappers._validate_recorded_command(spec, layout, config)


def test_start_identity_update_rejects_intervening_release_publication(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An A start cannot replace the config after release B has committed."""

    _published(layout_dirs, "release-a")
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    layout = wrappers.resolve_layout(**layout_dirs)
    original_write = wrappers._atomic_write_json
    winner: dict[Path, wrappers.FileSnapshot] = {}

    def publish_b_before_runtime_write(
        path: Path, value: object, **kwargs: object
    ) -> None:
        _published(layout_dirs, "release-b")
        winner.update(wrappers.capture_wrapper_snapshot(**layout_dirs))
        original_write(path, value, **kwargs)

    monkeypatch.setattr(wrappers, "_atomic_write_json", publish_b_before_runtime_write)
    with pytest.raises(wrappers.WrapperError, match="changed after preflight"):
        wrappers._record_start_identity(
            spec,
            layout,
            "release-a",
            wrappers.build_start_command(spec, **layout_dirs),
        )

    assert winner
    assert wrappers.capture_wrapper_snapshot(**layout_dirs) == winner
    assert wrappers.readback_wrappers(
        **layout_dirs, expected_release_id="release-b"
    )["ok"] is True


def test_runtime_config_snapshot_rejects_identical_replacement_leaf(
    layout_dirs: dict[str, Path],
) -> None:
    """Matching bytes do not authorize an update to a different config inode."""

    _published(layout_dirs)
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    path = wrappers.resolve_layout(**layout_dirs).config_path(spec)
    config, snapshot = wrappers._read_unit_config_snapshot(path, spec=spec)
    replacement = path.with_name("replacement-config")
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(wrappers.UNIT_CONFIG_MODE)
    replacement.replace(path)
    winner = path.stat()
    content = path.read_bytes()
    config["release_id"] = "release-b"

    with pytest.raises(wrappers.WrapperError, match="changed after preflight"):
        wrappers._atomic_write_json(
            path, config, mode=wrappers.UNIT_CONFIG_MODE, snapshot=snapshot
        )

    assert path.read_bytes() == content
    assert path.stat().st_ino == winner.st_ino
    assert path.stat().st_ctime_ns == winner.st_ctime_ns


def test_runtime_config_snapshot_rejects_replaced_parent(
    layout_dirs: dict[str, Path],
) -> None:
    """A stale runtime update cannot enter a replacement state directory."""

    _published(layout_dirs)
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    path = wrappers.resolve_layout(**layout_dirs).config_path(spec)
    config, snapshot = wrappers._read_unit_config_snapshot(path, spec=spec)
    content = path.read_bytes()
    original_parent = path.parent.with_name("original-state")
    path.parent.rename(original_parent)
    path.parent.mkdir()
    path.write_bytes(content)
    path.chmod(wrappers.UNIT_CONFIG_MODE)
    winner = path.stat()
    config["release_id"] = "release-b"

    with pytest.raises(wrappers.WrapperError, match="parent identity changed"):
        wrappers._atomic_write_json(
            path, config, mode=wrappers.UNIT_CONFIG_MODE, snapshot=snapshot
        )

    assert path.read_bytes() == content
    assert path.stat().st_ino == winner.st_ino
    assert path.stat().st_ctime_ns == winner.st_ctime_ns
    assert (original_parent / path.name).read_bytes() == content


def test_start_identity_update_accepts_unchanged_config(
    layout_dirs: dict[str, Path],
) -> None:
    """A normal start still records a complete, manifest-valid identity."""

    _published(layout_dirs)
    spec = wrappers.UNIT_SPEC_BY_NAME["termdeck-web-recovery.service"]
    layout = wrappers.resolve_layout(**layout_dirs)
    command = wrappers.build_start_command(spec, **layout_dirs)

    wrappers._record_start_identity(spec, layout, "release-a", command)

    config = json.loads(layout.config_path(spec).read_bytes())
    assert config["state"] == "starting"
    assert config["pid"] == os.getpid()
    assert config["argv"] == list(command)
    assert wrappers.readback_wrappers(
        **layout_dirs, expected_release_id="release-a"
    )["ok"] is True


def test_checkpoint_nonreaping_wait_bounds_persistent_eintr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    calls: list[tuple[int, int, int]] = []

    def interrupted_waitid(kind: int, pid: int, flags: int) -> None:
        calls.append((kind, pid, flags))
        raise InterruptedError(errno.EINTR, "wait interrupted")

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(wrappers.os, "waitid", interrupted_waitid)
    monkeypatch.setattr(wrappers, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance))
    process = SimpleNamespace(pid=4321, args=("checkpoint",))
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        wrappers._wait_checkpoint_exit(process, timeout=0.12)
    assert caught.value.timeout == 0.12
    assert elapsed == pytest.approx(0.12)
    assert len(calls) == 4
    assert set(calls) == {(os.P_PID, 4321, os.WEXITED | os.WNOHANG | os.WNOWAIT)}


def test_checkpoint_external_reaping_never_authorizes_group_cleanup(
    layout_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4321, args=("checkpoint",), returncode=None)

    def externally_reaped(*_args: object) -> None:
        raise ChildProcessError(errno.ECHILD, "child was already reaped")

    monkeypatch.setattr(wrappers.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(wrappers.os, "waitid", externally_reaped)
    monkeypatch.setattr(
        wrappers, "_cleanup_checkpoint_process",
        lambda *_args: pytest.fail("ECHILD released the PID for reuse"),
    )
    assert wrappers.stop_unit("termdeck-checkpoint.service", **layout_dirs) == 127
