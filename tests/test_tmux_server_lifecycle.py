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
        "termdeck.tmux_server_lifecycle._socket_root_visible_to_user_manager",
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


def test_first_server_start_uses_direct_client_when_socket_mount_is_private():
    command = ["/usr/bin/tmux", "new-session", "-d", "-s", "demo"]
    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._socket_root_visible_to_user_manager",
        return_value=False,
    ):
        assert scoped_tmux_server_command(command) is None


def test_non_server_start_command_is_not_wrapped():
    with mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ):
        assert scoped_tmux_server_command(["/usr/bin/tmux", "list-sessions"]) is None


def test_existing_legacy_server_is_left_unchanged_without_user_manager():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._tmux_server_pid",
        return_value=4321,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        return_value=("S", 9876),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value="0::/gui.service",
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._dedicated_unit_is_safe",
        return_value=False,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=False,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._ensure_anchor_unit"
    ) as ensure_anchor, mock.patch(
        "termdeck.tmux_server_lifecycle._attach_process_to_unit"
    ) as attach:
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is False
    assert result.changed is False
    assert result.server_pid == 4321
    assert "unavailable" in result.detail
    ensure_anchor.assert_not_called()
    attach.assert_not_called()


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


def test_existing_server_already_in_dedicated_scope_is_a_noop():
    with mock.patch(
        "termdeck.tmux_server_lifecycle._tmux_server_pid",
        return_value=4321,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        return_value=("S", 9876),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        return_value=(
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "t1000-tmux-server-12-abc.scope"
        ),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._dedicated_unit_is_safe",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._ensure_anchor_unit"
    ) as ensure_anchor:
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is True
    assert result.changed is False
    assert result.server_pid == 4321
    assert result.unit == "t1000-tmux-server-12-abc.scope"
    ensure_anchor.assert_not_called()


def test_existing_legacy_server_moves_to_delegated_anchor_and_revalidates_pid():
    cgroups = [
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/gui.service",
        (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "t1000-tmux-anchor-4321-9876.service"
        ),
    ]
    properties = {
        "Id": "t1000-tmux-anchor-4321-9876.service",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "9999",
        "ControlGroup": (
            "/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "t1000-tmux-anchor-4321-9876.service"
        ),
        "Delegate": "yes",
        "KillMode": "process",
    }

    with mock.patch(
        "termdeck.tmux_server_lifecycle._tmux_server_pid",
        side_effect=[4321, 4321, 4321],
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        side_effect=[("S", 9876), ("S", 9876), ("S", 9876)],
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        side_effect=cgroups,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._ensure_anchor_unit",
        return_value=(properties, True),
    ) as ensure_anchor, mock.patch(
        "termdeck.tmux_server_lifecycle._attach_process_to_unit"
    ) as attach, mock.patch(
        "termdeck.tmux_server_lifecycle._dedicated_unit_is_safe",
        side_effect=[False, True],
    ):
        result = isolate_existing_tmux_server("/usr/bin/tmux")

    assert result.ok is True
    assert result.changed is True
    assert result.unit == "t1000-tmux-anchor-4321-9876.service"
    ensure_anchor.assert_called_once_with(
        "t1000-tmux-anchor-4321-9876.service",
        server_pid=4321,
        server_start_ticks=9876,
        timeout=2.0,
    )
    attach.assert_called_once_with(
        "t1000-tmux-anchor-4321-9876.service",
        4321,
        timeout=2.0,
    )


def test_existing_server_isolation_rejects_pid_reuse_after_attachment():
    properties = {
        "Id": "t1000-tmux-anchor-4321-9876.service",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "9999",
        "ControlGroup": "/anchor",
        "Delegate": "yes",
        "KillMode": "process",
    }
    with mock.patch(
        "termdeck.tmux_server_lifecycle._tmux_server_pid",
        side_effect=[4321, 4321],
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_identity",
        side_effect=[("S", 9876), ("S", 9999)],
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._process_cgroup",
        side_effect=["0::/gui.service", "0::/t1000-tmux-anchor-4321-9876.service"],
    ), mock.patch(
        "termdeck.tmux_server_lifecycle.user_systemd_scope_available",
        return_value=True,
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._ensure_anchor_unit",
        return_value=(properties, True),
    ), mock.patch(
        "termdeck.tmux_server_lifecycle._attach_process_to_unit"
    ) as attach, mock.patch(
        "termdeck.tmux_server_lifecycle._stop_anchor_unit"
    ) as stop_anchor, mock.patch(
        "termdeck.tmux_server_lifecycle._dedicated_unit_is_safe",
        return_value=False,
    ):
        with pytest.raises(TmuxServerIsolationError, match="identity changed"):
            isolate_existing_tmux_server("/usr/bin/tmux")

    attach.assert_not_called()
    stop_anchor.assert_called_once_with(
        "t1000-tmux-anchor-4321-9876.service", timeout=2.0
    )


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
    assert child["TMUX_TMPDIR"] != str(runtime)
    assert "TMUX" not in child
    assert runtime.is_dir()
    assert runtime.stat().st_dev == tmp_path.stat().st_dev
    binding = storage_fabric._PRODUCTION_TMUX_RUNTIME_BINDING
    assert isinstance(binding, storage_fabric._ProductionDirectoryBinding)
    binding.close()
