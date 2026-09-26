from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import termdeck.storage_fabric as storage_fabric
from termdeck.tmux_server_lifecycle import (
    TmuxServerIsolationError,
    isolate_existing_tmux_server,
    process_exit_requires_tmux_isolation,
    scoped_tmux_server_command,
)


def test_main_process_of_group_killing_service_requires_isolation():
    properties = {
        "Id": "t1000-gui.service",
        "MainPID": "4321",
        "ControlGroup": "/app.slice/t1000-gui.service",
        "KillMode": "control-group",
    }
    with mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value="0::/app.slice/t1000-gui.service",
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._unit_properties",
        return_value=properties,
    ):
        assert process_exit_requires_tmux_isolation(pid=4321)


def test_child_of_service_main_process_may_exit_without_isolation():
    properties = {
        "Id": "validation.service",
        "MainPID": "1111",
        "ControlGroup": "/app.slice/validation.service",
        "KillMode": "control-group",
    }
    with mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value="0::/app.slice/validation.service",
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._unit_properties",
        return_value=properties,
    ):
        assert not process_exit_requires_tmux_isolation(pid=4321)


def test_same_cgroup_parent_is_safe_fallback_without_user_manager():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value="0::/app.slice/validation.service",
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._unit_properties",
        return_value=None,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._has_same_cgroup_ancestor",
        return_value=True,
    ):
        assert not process_exit_requires_tmux_isolation(pid=4321)


def test_service_root_is_conservative_fallback_without_user_manager():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value="0::/app.slice/t1000-gui.service",
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._unit_properties",
        return_value=None,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._has_same_cgroup_ancestor",
        return_value=False,
    ):
        assert process_exit_requires_tmux_isolation(pid=4321)


def test_first_server_start_is_wrapped_in_a_delegated_user_scope():
    command = ["/usr/bin/tmux", "new-session", "-d", "-s", "demo", "/bin/bash"]

    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.time.monotonic_ns",
        return_value=0xABC,
    ):
        wrapped = scoped_tmux_server_command(command, owner_pid=123)

    assert wrapped is not None
    assert wrapped[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    ]
    assert "--unit=t1000-tmux-server-123-abc.scope" in wrapped
    assert "--property=Delegate=yes" in wrapped
    assert wrapped[-len(command) :] == command


def test_first_server_start_falls_back_directly_without_user_manager():
    command = ["/usr/bin/tmux", "new-session", "-d", "-s", "demo"]
    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=False,
    ):
        assert scoped_tmux_server_command(command) is None


def test_first_server_start_keeps_scope_when_socket_mount_is_private(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/private-caller-mount/tmux")
    command = ["/usr/bin/tmux", "new-session", "-d", "-s", "demo"]
    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.Path.is_dir",
        return_value=False,
    ), mock.patch("termdeck.tmux_server_lifecycle._run") as manager_probe:
        wrapped = scoped_tmux_server_command(command)

    assert wrapped is not None
    assert wrapped[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]
    assert wrapped[-len(command):] == command
    manager_probe.assert_not_called()


def test_non_server_start_command_is_not_wrapped():
    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ):
        assert scoped_tmux_server_command(["/usr/bin/tmux", "list-sessions"]) is None


@pytest.mark.parametrize("manager_available", [False, True])
@pytest.mark.parametrize("unit", ["gui.service", "t1000-tmux-anchor-4321-9876.service"])
def test_legacy_server_refusal_preserves_identity_without_create_move_or_stop(
    manager_available, unit,
):
    cgroup = f"0::/app.slice/{unit}"
    queries = []

    def read_only_query(argv, *, timeout):
        queries.append(list(argv))
        if argv == ["/usr/bin/tmux", "display-message", "-p", "#{pid}"]:
            return subprocess.CompletedProcess(argv, 0, "4321\n", "")
        if argv[:4] == ["/usr/bin/systemctl", "--user", "show", unit]:
            # A matching name alone cannot admit an inactive legacy anchor.
            return subprocess.CompletedProcess(
                argv, 0,
                f"Id={unit}\nControlGroup=/app.slice/{unit}\n"
                "ActiveState=inactive\nSubState=dead\nDelegate=yes\n"
                "KillMode=process\nMainPID=0\n", "",
            )
        raise AssertionError(f"refused migration attempted a mutation: {argv}")

    with mock.patch(
        "termdeck.tmux_server_lifecycle._run", side_effect=read_only_query,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        return_value=("S", 9876),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value=cgroup,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=manager_available,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.os.kill",
    ) as signal_process:
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is False
    assert result.server_present is True
    assert result.changed is False
    assert result.server_pid == 4321
    assert result.server_start_ticks == 9876
    assert result.unit == unit
    assert result.cgroup == cgroup
    assert result.detail.startswith("recovery-blocked:")
    assert "unavailable" in result.detail
    assert "left in its current cgroup" in result.detail
    if manager_available:
        assert "generation-bound migration" in result.detail
        assert "Keep its owning service running" in result.detail
    assert len(queries) == (2 if unit.startswith("t1000-tmux-anchor-") else 1)
    signal_process.assert_not_called()


def test_safe_unit_requires_matching_id_and_control_group():
    from termdeck.tmux_server_lifecycle import _anchor_properties_are_safe

    properties = {
        "Id": "t1000-tmux-anchor-4321-9876.service",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "9999",
        "ControlGroup": "/app.slice/foreign.service",
        "Delegate": "yes",
        "KillMode": "process",
    }
    assert not _anchor_properties_are_safe(
        properties, "t1000-tmux-anchor-4321-9876.service"
    )


@pytest.mark.parametrize("unit", [
    "t1000-tmux-server-12-abc.scope",
    "t1000-tmux-anchor-4321-9876.service",
    "t1000-tmux-anchor.service",
])
def test_existing_server_in_verified_dedicated_unit_is_a_noop(unit):
    cgroup = f"0::/app.slice/{unit}"
    queries = []

    def read_only_query(argv, *, timeout):
        queries.append(list(argv))
        if argv == ["/usr/bin/tmux", "display-message", "-p", "#{pid}"]:
            return subprocess.CompletedProcess(argv, 0, "4321\n", "")
        if argv[:4] == ["/usr/bin/systemctl", "--user", "show", unit]:
            return subprocess.CompletedProcess(
                argv, 0,
                f"Id={unit}\nControlGroup=/app.slice/{unit}\n"
                "ActiveState=active\nSubState=running\nDelegate=yes\n"
                "KillMode=process\nMainPID=9999\n", "",
            )
        raise AssertionError(f"dedicated server admission attempted a mutation: {argv}")

    with mock.patch(
        "termdeck.tmux_server_lifecycle._run", side_effect=read_only_query,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        return_value=("S", 9876),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value=cgroup,
    ):
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is True
    assert result.server_present is True
    assert result.changed is False
    assert result.server_pid == 4321
    assert result.server_start_ticks == 9876
    assert result.unit == unit
    assert result.cgroup == cgroup
    assert len(queries) == 2


def test_absent_server_admission_does_not_create_a_replacement():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._run",
        return_value=subprocess.CompletedProcess([], 1, "", "no server running"),
    ) as query, mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
    ) as identity:
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is True
    assert result.server_present is False
    assert result.changed is False
    assert result.server_pid is None
    query.assert_called_once_with(
        ["/usr/bin/tmux", "display-message", "-p", "#{pid}"], timeout=0.35,
    )
    identity.assert_not_called()


def test_unavailable_server_identity_is_rejected_without_mutation():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._tmux_server_pid",
        return_value=4321,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        return_value=None,
    ), mock.patch("termdeck.tmux_server_lifecycle._run") as command:
        with pytest.raises(TmuxServerIsolationError, match="identity is unavailable"):
            isolate_existing_tmux_server("/usr/bin/tmux")

    command.assert_not_called()


def test_tmux_backend_run_uses_direct_command_when_scope_is_unavailable():
    from termdeck.tmux_backend import TmuxBackend

    completed = subprocess.CompletedProcess(
        args=["/usr/bin/tmux", "new-session"],
        returncode=0,
        stdout="",
        stderr="",
    )
    backend = TmuxBackend(executable="/usr/bin/tmux")
    with mock.patch(
        "termdeck.tmux_backend.scoped_tmux_server_command",
        return_value=None,
    ), mock.patch(
        "termdeck.tmux_backend.subprocess.run",
        return_value=completed,
    ) as run, mock.patch(
        "termdeck.tmux_backend.isolate_existing_tmux_server",
        side_effect=[
            SimpleNamespace(server_present=False),
            SimpleNamespace(server_present=True),
        ],
    ) as isolate:
        result = backend._run(["new-session"])

    assert result is completed
    assert run.call_args.args[0] == ["/usr/bin/tmux", "new-session"]
    assert isolate.call_count == 2
    isolate.assert_has_calls(
        [
            mock.call("/usr/bin/tmux", query_timeout=0.35, timeout=2.0),
            mock.call("/usr/bin/tmux", query_timeout=0.35, timeout=2.0),
        ]
    )


def test_tmux_backend_existing_server_skips_transient_client_scope():
    from termdeck.tmux_backend import TmuxBackend

    completed = subprocess.CompletedProcess(
        args=["/usr/bin/tmux", "new-session"],
        returncode=0,
        stdout="",
        stderr="",
    )
    backend = TmuxBackend(executable="/usr/bin/tmux")
    with mock.patch(
        "termdeck.tmux_backend.isolate_existing_tmux_server",
        side_effect=[
            SimpleNamespace(server_present=True),
            SimpleNamespace(server_present=True),
        ],
    ) as isolate, mock.patch(
        "termdeck.tmux_backend.scoped_tmux_server_command"
    ) as wrap, mock.patch(
        "termdeck.tmux_backend.subprocess.run",
        return_value=completed,
    ) as run:
        result = backend._run(["new-session"])

    assert result is completed
    wrap.assert_not_called()
    assert run.call_args.args[0] == ["/usr/bin/tmux", "new-session"]
    assert isolate.call_count == 2


def test_tmux_backend_run_uses_scope_wrapper_for_new_session():
    from termdeck.tmux_backend import TmuxBackend

    completed = subprocess.CompletedProcess(
        args=["systemd-run"], returncode=0, stdout="", stderr=""
    )
    backend = TmuxBackend(executable="/usr/bin/tmux")
    with mock.patch(
        "termdeck.tmux_backend.scoped_tmux_server_command",
        return_value=["/usr/bin/systemd-run", "--scope", "/usr/bin/tmux", "new-session"],
    ) as wrap, mock.patch(
        "termdeck.tmux_backend.subprocess.run",
        return_value=completed,
    ) as run, mock.patch(
        "termdeck.tmux_backend.isolate_existing_tmux_server",
        side_effect=[
            SimpleNamespace(server_present=False),
            SimpleNamespace(server_present=True),
        ],
    ) as isolate:
        result = backend._run(["new-session"])

    assert result is completed
    wrap.assert_called_once_with(["/usr/bin/tmux", "new-session"])
    assert run.call_args.args[0][0] == "/usr/bin/systemd-run"
    assert isolate.call_count == 2
    isolate.assert_has_calls(
        [
            mock.call("/usr/bin/tmux", query_timeout=0.35, timeout=2.0),
            mock.call("/usr/bin/tmux", query_timeout=0.35, timeout=2.0),
        ]
    )


def test_tmux_backend_child_uses_exact_attested_socket_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from termdeck.tmux_backend import TmuxBackend

    runtime = tmp_path / "tmux-runtime"
    device = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    monkeypatch.setattr(
        storage_fabric,
        "_PRODUCTION_TMUX_RUNTIME_BINDING",
        (runtime, device),
    )
    monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(runtime))
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    completed = subprocess.CompletedProcess(
        args=["/usr/bin/tmux", "list-sessions"],
        returncode=0,
        stdout="",
        stderr="",
    )
    backend = TmuxBackend(executable="/usr/bin/tmux")

    with mock.patch(
        "termdeck.tmux_backend.subprocess.run",
        return_value=completed,
    ) as run:
        result = backend._run(["list-sessions"])

    assert result is completed
    child = run.call_args.kwargs["env"]
    assert child["TERMDECK_TMUX_RUNTIME"] == str(runtime)
    assert Path(child["TMUX_TMPDIR"]).samefile(runtime)
    assert child["TMUX_TMPDIR"] == str(runtime)
    assert "TMUX" not in child
    assert runtime.is_dir()
    assert runtime.stat().st_dev == tmp_path.stat().st_dev
    binding = storage_fabric._PRODUCTION_TMUX_RUNTIME_BINDING
    assert isinstance(binding, storage_fabric._ProductionDirectoryBinding)
    binding.close()
