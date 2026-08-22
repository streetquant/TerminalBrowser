from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from termdeck.models import SessionRecord
from termdeck.session_recipe import record_recipe_fields
from termdeck.tmux_backend import TmuxBackend, TmuxError, _PaneLifecycle


def _record(
    *,
    command: str = "python worker.py",
    cwd: str = "/tmp",
    status: str = "recovery-pending",
    restart_policy: str = "on-failure",
    environment: dict[str, str] | None = None,
    external: bool = False,
) -> SessionRecord:
    recipe = digest = None
    policy = restart_policy
    if not external:
        recipe, digest, policy = record_recipe_fields(
            command=command,
            cwd=cwd,
            environment=environment,
            restart_policy=restart_policy,
            origin="launch",
        )
    return SessionRecord(
        id="td-generic",
        name="generic",
        tag="work",
        command=command,
        cwd=cwd,
        tmux_name="termdeck_generic",
        created_at="now",
        last_opened_at="now",
        status=status,
        external=external,
        recovery_recipe=recipe,
        recovery_recipe_sha256=digest,
        restart_policy=policy,
    )


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tmux"], 0, "", "")


class GenericTmuxRecoveryTests(unittest.TestCase):
    def _without_checkpoint(self, backend: TmuxBackend):
        return (
            mock.patch.object(backend.checkpoints, "restart_command", return_value=None),
            mock.patch.object(
                backend.checkpoints,
                "wrap_launch_command",
                side_effect=lambda _record, command: command,
            ),
        )

    def test_missing_arbitrary_command_replays_generic_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _record(command="python worker.py --queue alpha", cwd=tmp)
            backend = TmuxBackend(executable="tmux")
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=False), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertEqual(launch[:4], ["new-session", "-d", "-s", record.tmux_name])
            self.assertEqual(launch[-3:-1], ["-c", str(Path(tmp).resolve())])
            self.assertIn(record.command, launch[-1])
            self.assertIn("__termdeck_status=$?", launch[-1])
            self.assertIn("interactive shell ready", launch[-1])
            self.assertIn("exec ", launch[-1])

    def test_opaque_application_resume_command_needs_no_known_tool_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = SessionRecord(
                id="td-opaque",
                name="opaque",
                tag="work",
                command="any-tui",
                cwd=tmp,
                tmux_name="termdeck_opaque",
                created_at="now",
                last_opened_at="now",
                status="recovery-pending",
                recovery_command="any-tui --resume durable-job-17",
                recovery_cwd=tmp,
            )
            backend = TmuxBackend(executable="tmux")
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=False), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)
            launch_command = run.call_args_list[0].args[0][-1]
            self.assertIn(record.recovery_command, launch_command)
            self.assertIn("__termdeck_status=$?", launch_command)
            self.assertIn("exec ", launch_command)

    def test_recipe_environment_and_missing_cwd_are_recovered_without_creating_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "unmounted" / "project"
            record = _record(
                command="service --foreground",
                cwd=str(missing),
                environment={"MODE": "production", "PORT": "9000"},
            )
            backend = TmuxBackend(executable="tmux")
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=False), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertFalse(missing.exists())
            self.assertIn("MODE=production", launch)
            self.assertIn("PORT=9000", launch)
            self.assertIn("TERMDECK_RECOVERY_DEGRADED=missing-working-directory", launch)
            self.assertIn(f"TERMDECK_RECOVERY_ORIGINAL_CWD={missing}", launch)
            self.assertEqual(launch[launch.index("-c") + 1], str(Path(tmp).resolve()))

    def test_corrupted_recipe_fails_before_tmux_mutation(self) -> None:
        record = _record()
        record = SessionRecord(
            **{
                **record.__dict__,
                "recovery_recipe": "{}",
                "recovery_recipe_sha256": "0" * 64,
            }
        )
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend, "_run"
        ) as run, self.assertRaisesRegex(TmuxError, "durable session recipe is invalid"):
            backend.create_or_resume(record)
        run.assert_not_called()

    def test_missing_manual_session_is_not_automatically_recreated(self) -> None:
        record = _record(restart_policy="manual")
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend, "_run"
        ) as run, self.assertRaisesRegex(TmuxError, "does not authorize missing-session recovery"):
            backend.create_or_resume(record)
        run.assert_not_called()

    def test_external_session_is_never_implicitly_impersonated(self) -> None:
        record = _record(external=True)
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend, "_run"
        ) as run, self.assertRaisesRegex(TmuxError, "adopted tmux session not found"):
            backend.create_or_resume(record)
        run.assert_not_called()

    def test_dead_pane_with_nonzero_status_is_respawned_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _record(command="python worker.py", cwd=tmp, status="running")
            backend = TmuxBackend(executable="tmux")
            lifecycle = _PaneLifecycle("%42", True, 137, tmp)
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=True), mock.patch.object(
                backend, "_pane_lifecycle", return_value=lifecycle
            ), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)

            respawn = run.call_args_list[0].args[0]
            self.assertEqual(respawn[:5], ["respawn-pane", "-k", "-t", "%42", "-c"])
            self.assertIn(record.command, respawn[-1])
            self.assertIn("__termdeck_status=$?", respawn[-1])
            self.assertIn("interactive shell ready", respawn[-1])
            self.assertNotIn("kill-session", respawn)


    def test_direct_interactive_shell_is_not_double_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _record(command="/bin/bash -l", cwd=tmp, status="new")
            backend = TmuxBackend(executable="tmux")
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=False), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)

            self.assertEqual(run.call_args_list[0].args[0][-1], record.command)

    def test_shell_command_string_is_supervised_like_an_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _record(command="/bin/bash -lc 'printf done'", cwd=tmp, status="new")
            backend = TmuxBackend(executable="tmux")
            checkpoint, wrapper = self._without_checkpoint(backend)
            with mock.patch.object(backend, "session_exists", return_value=False), checkpoint, wrapper, mock.patch.object(
                backend, "_run", return_value=_completed()
            ) as run:
                backend.create_or_resume(record)

            launch_command = run.call_args_list[0].args[0][-1]
            self.assertIn(record.command, launch_command)
            self.assertIn("__termdeck_status=$?", launch_command)
            self.assertIn("interactive shell ready", launch_command)

    def test_supervisor_preserves_nonzero_failure_for_on_failure_restart(self) -> None:
        command = TmuxBackend._with_interactive_shell_fallback("exit 17", "/bin/sh -i")
        self.assertIn('exit "$__termdeck_status"', command)
        self.assertIn('"$__termdeck_status" -eq 0', command)
        self.assertNotIn('"$__termdeck_status" -eq 17', command)

    def test_on_failure_policy_leaves_successfully_exited_pane_stopped(self) -> None:
        record = _record(status="running", restart_policy="on-failure")
        backend = TmuxBackend(executable="tmux")
        lifecycle = _PaneLifecycle("%42", True, 0, "/tmp")
        with mock.patch.object(backend, "session_exists", return_value=True), mock.patch.object(
            backend, "_pane_lifecycle", return_value=lifecycle
        ), mock.patch.object(backend, "_run") as run:
            backend.create_or_resume(record)
        run.assert_not_called()

    def test_live_pane_is_idempotent_and_never_relaunched(self) -> None:
        record = _record(status="running")
        backend = TmuxBackend(executable="tmux")
        lifecycle = _PaneLifecycle("%42", False, None, "/tmp")
        with mock.patch.object(backend, "session_exists", return_value=True), mock.patch.object(
            backend, "_pane_lifecycle", return_value=lifecycle
        ), mock.patch.object(backend, "_run") as run:
            backend.create_or_resume(record)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
