from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock

import pytest

from termdeck.gateway_models import GatewayModelStatus
from termdeck.models import SessionRecord
from termdeck.session_recipe import build_session_recipe, parse_session_recipe
from termdeck.tmux_backend import (
    TmuxBackend,
    TmuxError,
    _AuthPaneIdentity,
    _LaunchSpec,
)


SESSION_ID = "d2a7ed1b-d6fd-45de-8342-5f10cfed3c07"


def _record(cwd: str) -> SessionRecord:
    return SessionRecord(
        id="td-models",
        name="models",
        tag="work",
        command="cc-go-ulw",
        cwd=cwd,
        tmux_name="termdeck_models",
        created_at="now",
        last_opened_at="now",
        status="running",
        recovery_command=f"cc-go-ulw --resume {SESSION_ID}",
        recovery_cwd=cwd,
    )


def test_backend_status_uses_live_pane_start_epoch(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend()
    expected = GatewayModelStatus(True, True, True, model_count=43)
    with mock.patch.object(backend, "pane_pid", return_value=321), mock.patch(
        "termdeck.tmux_backend.process_start_epoch", return_value=1000.0
    ) as started, mock.patch(
        "termdeck.tmux_backend.classify_gateway_model_status", return_value=expected
    ) as classify:
        status = backend.gateway_model_status(record, cache_path=tmp_path / "models.json")

    assert status is expected
    started.assert_called_once_with(321)
    classify.assert_called_once_with(
        record,
        process_started_at=1000.0,
        cache_path=tmp_path / "models.json",
    )



def test_nonprivate_exact_readiness_does_not_require_private_auth_environment(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend()
    previous = _AuthPaneIdentity("created", "%42", 100, "claude", str(tmp_path))
    created = _AuthPaneIdentity("created", "%42", 200, "sh", str(tmp_path))
    exact = record.recovery_command
    with mock.patch.object(backend, "_inspect_auth_pane", return_value=created), mock.patch.object(
        backend, "_auth_process_identity", return_value=("S", 999)
    ), mock.patch.object(
        backend,
        "_private_auth_process_tree_environment_ready",
        side_effect=AssertionError("non-private refresh consulted private auth environment"),
    ), mock.patch(
        "termdeck.tmux_backend.observe_recovery_metadata",
        return_value=(True, exact, str(tmp_path)),
    ):
        observed, error = backend._await_exact_pane_respawn_ready(
            record,
            previous=previous,
            expected_recovery_command=exact,
            expected_recovery_cwd=str(tmp_path),
            timeout=0.05,
        )
    assert observed == created
    assert error == ""

def test_exact_live_payload_refresh_respawns_same_pane_and_waits_for_resume(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend(executable="tmux")
    exact = record.recovery_command
    before = _AuthPaneIdentity("created", "%42", 100, "claude", str(tmp_path))
    after = _AuthPaneIdentity("created", "%42", 200, "claude", str(tmp_path))
    spec = _LaunchSpec(
        recipe=build_session_recipe(exact, tmp_path, origin="native-resume"),
        command=exact,
        directory=tmp_path,
        environment=(("MODEL_CACHE", "fresh"),),
        application_native_resume=True,
    )

    with mock.patch.object(backend, "_inspect_auth_pane", side_effect=[before, before]), mock.patch.object(
        backend, "_auth_process_identity", return_value=("S", 123)
    ), mock.patch(
        "termdeck.tmux_backend.observe_recovery_metadata",
        return_value=(True, exact, str(tmp_path)),
    ), mock.patch.object(
        backend, "_disable_auth_pane_input", return_value=False
    ) as disable, mock.patch.object(
        backend, "_suspend_auth_process_tree", return_value=((100, 123, False),)
    ) as suspend, mock.patch.object(
        backend, "_launch_spec", return_value=spec
    ), mock.patch.object(
        backend, "_run"
    ) as run, mock.patch.object(
        backend, "_kill_quiesced_auth_processes"
    ) as kill_old, mock.patch.object(
        backend, "_await_exact_pane_respawn_ready", return_value=(after, "")
    ) as await_ready, mock.patch.object(
        backend, "_restore_auth_pane_input"
    ) as restore_input:
        pid = backend.restart_exact_payload(record)

    assert pid == 200
    disable.assert_called_once_with("%42")
    suspend.assert_called_once_with(100, allow_running_root=True)
    respawn = run.call_args_list[0].args[0]
    assert respawn[:4] == ["respawn-pane", "-k", "-t", "%42"]
    assert "MODEL_CACHE=fresh" in respawn
    assert respawn[-3:] == ["-c", str(tmp_path), exact]
    kill_old.assert_called_once_with(((100, 123, False),))
    await_ready.assert_called_once_with(
        record,
        previous=before,
        expected_recovery_command=exact,
        expected_recovery_cwd=str(tmp_path),
    )
    restore_input.assert_called_once_with("%42", False)


def test_exact_live_payload_refresh_refuses_mismatched_conversation_before_mutation(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend(executable="tmux")
    before = _AuthPaneIdentity("created", "%42", 100, "claude", str(tmp_path))
    with mock.patch.object(backend, "_inspect_auth_pane", return_value=before), mock.patch.object(
        backend, "_auth_process_identity", return_value=("S", 123)
    ), mock.patch(
        "termdeck.tmux_backend.observe_recovery_metadata",
        return_value=(True, "cc-go-ulw --resume another-session", str(tmp_path)),
    ), mock.patch.object(backend, "_run") as run:
        with pytest.raises(TmuxError, match="different exact recovery"):
            backend.restart_exact_payload(record)
    run.assert_not_called()


def test_clean_exit_bridges_old_payload_to_shell_without_resuming_agent(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend(executable="tmux")
    lifecycle = __import__("termdeck.tmux_backend", fromlist=["_PaneLifecycle"])._PaneLifecycle(
        "%42", True, 0, str(tmp_path)
    )
    shell_recipe = build_session_recipe("/bin/bash -i", tmp_path, origin="legacy-launch")
    shell_spec = _LaunchSpec(
        recipe=shell_recipe,
        command="/bin/bash -i",
        directory=tmp_path,
        environment=(("SHELL_MODE", "fallback"),),
    )
    with mock.patch.object(backend, "_pane_lifecycle", return_value=lifecycle), mock.patch.object(
        backend, "_launch_spec", return_value=shell_spec
    ) as launch_spec, mock.patch.object(backend, "_run") as run, mock.patch.object(
        backend, "_await_live_pane_pid", return_value=222
    ) as await_pid, mock.patch.object(
        backend, "create_or_resume"
    ) as create:
        result = backend.recover_dead_payload(record)

    assert result.action == "shell"
    assert result.pid == 222
    assert result.command == "/usr/bin/bash -i" or result.command == "/bin/bash -i"
    assert result.cwd == str(tmp_path)
    assert result.clear_recovery
    derived = launch_spec.call_args.args[0]
    assert derived.recovery_command is None
    assert derived.recovery_cwd is None
    assert derived.recovery_recipe is not None
    assert derived.recovery_recipe_sha256 is not None
    derived_recipe = parse_session_recipe(
        derived.recovery_recipe,
        derived.recovery_recipe_sha256,
    )
    assert derived_recipe.command == result.command
    assert derived_recipe.origin == "native-resume"
    assert derived.status == "new"
    respawn = run.call_args_list[0].args[0]
    assert respawn[:4] == ["respawn-pane", "-k", "-t", "%42"]
    assert "SHELL_MODE=fallback" in respawn
    assert respawn[-3:] == ["-c", str(tmp_path), "/bin/bash -i"]
    await_pid.assert_called_once_with(record, pane_id="%42")
    create.assert_not_called()


@pytest.mark.parametrize("exit_status", [1, 17, 137])
def test_failed_dead_payload_resumes_durable_recipe(
    tmp_path: Path, exit_status: int
) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend(executable="tmux")
    lifecycle = __import__("termdeck.tmux_backend", fromlist=["_PaneLifecycle"])._PaneLifecycle(
        "%42", True, exit_status, str(tmp_path)
    )
    with mock.patch.object(backend, "_pane_lifecycle", return_value=lifecycle), mock.patch.object(
        backend, "create_or_resume"
    ) as create, mock.patch.object(backend, "pane_pid", return_value=333):
        result = backend.recover_dead_payload(record)

    assert result.action == "restarted"
    assert result.pid == 333
    assert not result.clear_recovery
    probe = create.call_args.args[0]
    assert probe.status == "recovery-pending"
    assert probe.recovery_command == record.recovery_command


def test_ctrl_c_exit_uses_same_shell_bridge_as_clean_exit(tmp_path: Path) -> None:
    record = _record(str(tmp_path))
    backend = TmuxBackend(executable="tmux")
    lifecycle = __import__("termdeck.tmux_backend", fromlist=["_PaneLifecycle"])._PaneLifecycle(
        "%42", True, 130, str(tmp_path)
    )
    spec = _LaunchSpec(
        recipe=build_session_recipe("/bin/sh -i", tmp_path),
        command="/bin/sh -i",
        directory=tmp_path,
        environment=(),
    )
    with mock.patch.object(backend, "_pane_lifecycle", return_value=lifecycle), mock.patch.object(
        backend, "_launch_spec", return_value=spec
    ), mock.patch.object(backend, "_run"), mock.patch.object(
        backend, "_await_live_pane_pid", return_value=444
    ):
        result = backend.recover_dead_payload(record)
    assert result.action == "shell"
    assert result.pid == 444


def test_always_policy_restarts_even_after_clean_exit(tmp_path: Path) -> None:
    record = SessionRecord(**{**_record(str(tmp_path)).__dict__, "restart_policy": "always"})
    # Legacy fields are authoritative in this unit; clear the prebuilt recipe so
    # the changed policy is represented by the derived durable recipe.
    record = SessionRecord(
        **{
            **record.__dict__,
            "recovery_recipe": None,
            "recovery_recipe_sha256": None,
        }
    )
    backend = TmuxBackend(executable="tmux")
    lifecycle = __import__("termdeck.tmux_backend", fromlist=["_PaneLifecycle"])._PaneLifecycle(
        "%42", True, 0, str(tmp_path)
    )
    with mock.patch.object(backend, "_pane_lifecycle", return_value=lifecycle), mock.patch.object(
        backend, "create_or_resume"
    ) as create, mock.patch.object(backend, "pane_pid", return_value=555):
        result = backend.recover_dead_payload(record)
    assert result.action == "restarted"
    assert result.pid == 555
    create.assert_called_once()
