import subprocess
from pathlib import Path
import unittest
from unittest import mock

from termdeck.models import SessionRecord
from termdeck.tmux_backend import TmuxBackend, TmuxError


def _record(**kwargs) -> SessionRecord:
    base = dict(
        id="td-test",
        name="test",
        tag="dev",
        command="bash",
        cwd="/tmp",
        tmux_name="termdeck_dev_test",
        created_at="now",
        last_opened_at="now",
    )
    base.update(kwargs)
    return SessionRecord(**base)


class TmuxBackendTests(unittest.TestCase):
    def test_scroll_down_at_live_bottom_does_not_enter_history(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")

        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run") as run:
            handled = backend.scroll_session(record, 3)

        self.assertFalse(handled)
        self.assertFalse(backend.scroll_view_active(record))
        run.assert_not_called()

    def test_scroll_codex_opens_transcript_before_navigating(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        command = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="codex\n", stderr=""
        )
        scrolled = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", side_effect=[command, scrolled]
        ) as run:
            handled = backend.scroll_session(record, -3)

        self.assertTrue(handled)
        self.assertTrue(backend.scroll_view_active(record))
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        backend.pane_target(record),
                        "#{pane_current_command}",
                    ],
                    check=False,
                    timeout=backend.query_timeout,
                ),
                mock.call(
                    [
                        "send-keys",
                        "-t",
                        backend.pane_target(record),
                        "C-t",
                        ";",
                        "send-keys",
                        "-t",
                        backend.pane_target(record),
                        "-N",
                        "3",
                        "Up",
                    ],
                    check=False,
                    timeout=backend.query_timeout,
                ),
            ],
        )

    def test_scroll_shell_uses_tmux_copy_mode_not_child_arrow_keys(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        command = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="bash\n", stderr=""
        )
        scrolled = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", side_effect=[command, scrolled]
        ) as run:
            handled = backend.scroll_session(record, -3)

        self.assertTrue(handled)
        self.assertTrue(backend.scroll_view_active(record))
        self.assertEqual(
            run.call_args_list[-1],
            mock.call(
                [
                    "copy-mode",
                    "-t",
                    backend.pane_target(record),
                    ";",
                    "send-keys",
                    "-X",
                    "-t",
                    backend.pane_target(record),
                    "-N",
                    "3",
                    "scroll-up",
                ],
                check=False,
                timeout=backend.query_timeout,
            ),
        )

    def test_subsequent_codex_wheel_does_not_toggle_transcript_closed(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = "codex-transcript"
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            handled = backend.scroll_session(record, 4)

        self.assertTrue(handled)
        run.assert_called_once_with(
            [
                "send-keys",
                "-t",
                backend.pane_target(record),
                "-N",
                "4",
                "Down",
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_large_codex_scrollbar_jump_uses_page_navigation(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = "codex-transcript"
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            handled = backend.scroll_session(record, -96)

        self.assertTrue(handled)
        run.assert_called_once_with(
            [
                "send-keys",
                "-t",
                backend.pane_target(record),
                "-N",
                "4",
                "PageUp",
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_interactive_session_leaves_pointer_with_tui_and_bounds_history(self):
        record = _record()
        backend = TmuxBackend(executable="tmux", history_limit=2500)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.configure_interactive_session(record)

        run.assert_called_once_with(
            [
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-t",
                record.tmux_name,
                "history-limit",
                "2500",
                ";",
                "set-option",
                "-w",
                "-t",
                backend.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                backend.pane_target(record),
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_restore_live_session_disables_mouse_and_leaves_copy_mode(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.restore_live_session(record)

        run.assert_called_once_with(
            [
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-w",
                "-t",
                backend.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                backend.pane_target(record),
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_restore_live_codex_transcript_closes_it_before_live_input(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = "codex-transcript"
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.restore_live_session(record)

        self.assertFalse(backend.scroll_view_active(record))
        run.assert_called_once_with(
            [
                "send-keys",
                "-t",
                backend.pane_target(record),
                "C-t",
                ";",
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-w",
                "-t",
                backend.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                backend.pane_target(record),
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_terminate_forgets_tracked_scroll_view(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = "tmux-history"
        with mock.patch.object(backend, "session_exists", return_value=False):
            backend.terminate(record)

        self.assertFalse(backend.scroll_view_active(record))

    def test_interactive_configuration_failure_backs_off(self):
        first = _record(id="first", tmux_name="first")
        second = _record(id="second", tmux_name="second")
        backend = TmuxBackend(executable="tmux", failure_backoff=60)
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=124, stdout="", stderr="timeout")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ) as run:
            backend.configure_interactive_session(first)
            backend.configure_interactive_session(second)

        run.assert_called_once()

    def test_attach_command_shape(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        self.assertEqual(backend.attach_command(record), ["tmux", "attach-session", "-t", "termdeck_dev_test"])
        self.assertEqual(backend.pane_target(record), "termdeck_dev_test:0.0")

    def test_adopted_pane_target_overrides_default_target(self):
        record = _record(
            id="td-test",
            name="legacy",
            tag="adopted",
            tmux_name="legacy",
            pane_target="%7",
            external=True,
        )
        backend = TmuxBackend(executable="tmux")
        self.assertEqual(backend.pane_target(record), "%7")

    def test_current_directory_reads_live_pane_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            record = _record()
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(
                args=["tmux"],
                returncode=0,
                stdout=f"{tmp}\n",
                stderr="",
            )
            with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                result = backend.current_directory(record)

            self.assertEqual(result, str(Path(tmp).resolve()))
            run.assert_called_once_with(
                [
                    "display-message",
                    "-p",
                    "-t",
                    "termdeck_dev_test:0.0",
                    "#{pane_current_path}",
                ],
                check=False,
                timeout=backend.query_timeout,
            )

    def test_current_directories_share_batched_pane_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first_directory = Path(tmp) / "first"
            second_directory = Path(tmp) / "second"
            first_directory.mkdir()
            second_directory.mkdir()
            first = _record(id="a", tmux_name="sess_a")
            second = _record(id="b", tmux_name="sess_b", pane_target="%9", external=True)
            backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
            completed = subprocess.CompletedProcess(
                args=["tmux"],
                returncode=0,
                stdout=(
                    f"sess_a\t0\t0\t%3\t111\t{first_directory}\n"
                    f"sess_b\t1\t2\t%9\t222\t{second_directory}\n"
                ),
                stderr="",
            )
            with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                directories = backend.current_directories([first, second])
                pids = backend.pane_pids([first, second])

            self.assertEqual(
                directories,
                {"a": str(first_directory.resolve()), "b": str(second_directory.resolve())},
            )
            self.assertEqual(pids, {"a": 111, "b": 222})
            run.assert_called_once()

    def test_session_exists_many_uses_one_bounded_list_call(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", query_timeout=0.2)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_b\nunrelated\n",
            stderr="",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            result = backend.session_exists_many([first, second])

        self.assertEqual(result, {"a": False, "b": True})
        run.assert_called_once_with(
            ["list-sessions", "-F", "#{session_name}"],
            check=False,
            timeout=0.2,
        )

    def test_session_batch_timeout_preserves_prior_live_state(self):
        running = _record(id="live", status="running")
        stopped = _record(id="stopped", status="stopped")
        backend = TmuxBackend(executable="tmux")
        timed_out = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=124,
            stdout="",
            stderr="tmux command timed out",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=timed_out
        ):
            result = backend.session_exists_many([running, stopped])

        self.assertEqual(result, {"live": True, "stopped": False})

    def test_session_batch_failure_is_stale_not_dead(self):
        running = _record(id="live", status="running")
        stopped = _record(id="stopped", status="stopped")
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=1,
            stdout="",
            stderr="server busy",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ):
            result = backend.session_exists_many([running, stopped])

        self.assertEqual(result, {"live": True, "stopped": False})

    def test_pane_snapshot_timeout_does_not_expand_into_per_session_calls(self):
        first = _record(id="a", tmux_name="sess_a", last_pid=111)
        second = _record(id="b", tmux_name="sess_b", last_pid=222)
        backend = TmuxBackend(executable="tmux")
        timed_out = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=124,
            stdout="",
            stderr="tmux command timed out",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=timed_out
        ) as run:
            result = backend.pane_pids([first, second])

        self.assertEqual(result, {"a": 111, "b": 222})
        run.assert_called_once()

    def test_fleet_snapshot_failure_has_backoff_and_never_fans_out(self):
        records = [_record(id=f"td-{index}", tmux_name=f"sess-{index}", last_pid=index) for index in range(2000)]
        backend = TmuxBackend(executable="tmux", failure_backoff=60)
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="busy")

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ) as run:
            first = backend.pane_pids(records)
            second = backend.pane_pids(records)

        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["td-1999"], 1999)
        self.assertEqual(second, first)
        self.assertTrue(backend.pane_snapshot_stale)
        self.assertGreater(backend._query_timeout_for(2000), backend.query_timeout)
        self.assertLessEqual(backend._query_timeout_for(2000), backend.command_timeout)

    def test_capture_is_one_bounded_tmux_call(self):
        record = _record()
        backend = TmuxBackend(executable="tmux", max_capture_lines=500)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="tail\n", stderr="")
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            captured = backend.capture(record, lines=5000)

        self.assertEqual(captured, "tail\n")
        run.assert_called_once_with(
            ["capture-pane", "-t", backend.pane_target(record), "-p", "-S", "-500"],
            check=False,
            timeout=backend._query_timeout_for(500),
        )

    def test_run_returns_bounded_failure_for_nonchecking_query(self):
        backend = TmuxBackend(executable="tmux", command_timeout=0.1)
        expired = subprocess.TimeoutExpired(["tmux", "list-sessions"], 0.1)
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
            "termdeck.tmux_backend.subprocess.run", side_effect=expired
        ) as run:
            result = backend._run(["list-sessions"], check=False)

        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.1)

    def test_run_raises_clear_error_for_timed_out_command(self):
        backend = TmuxBackend(executable="tmux", command_timeout=0.1)
        expired = subprocess.TimeoutExpired(["tmux", "new-session"], 0.1)
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
            "termdeck.tmux_backend.subprocess.run", side_effect=expired
        ):
            with self.assertRaisesRegex(TmuxError, "timed out after 0.1s"):
                backend._run(["new-session"])

    def test_current_directory_returns_none_when_tmux_path_is_unavailable(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=1,
            stdout="",
            stderr="missing pane",
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ):
            self.assertIsNone(backend.current_directory(record))

    def test_pane_pid_uses_single_list_panes_call(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="termdeck_dev_test\t0\t0\tpane\t4321\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pid(record), 4321)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "list-panes")

    def test_pane_pid_reuses_shared_all_pane_cache(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\tpane-a\t111\nsess_b\t0\t0\tpane-b\t222\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pid(first), 111)
            self.assertEqual(backend.pane_pid(second), 222)
        run.assert_called_once()

    def test_pane_pids_batch_maps_default_and_percent_targets(self):
        default = _record(id="a", tmux_name="sess_a")
        adopted = _record(id="b", tmux_name="sess_b", pane_target="%9", external=True)
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout=(
                "sess_a\t0\t0\t%3\t111\n"
                "sess_b\t1\t2\t%9\t222\n"
            ),
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed):
            pids = backend.pane_pids([default, adopted])
        self.assertEqual(pids["a"], 111)
        self.assertEqual(pids["b"], 222)

    def test_pane_pids_falls_back_per_session_when_list_all_fails(self):
        record = _record(id="only")
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="no server")
        with mock.patch.object(backend, "_run", return_value=failed):
            with mock.patch.object(backend, "pane_pid", return_value=999) as single:
                pids = backend.pane_pids([record])
        single.assert_called_once_with(record)
        self.assertEqual(pids["only"], 999)

    def test_pane_pids_uses_short_lived_cache(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout=(
                "sess_a\t0\t0\t%3\t111\n"
                "sess_b\t0\t0\t%4\t222\n"
            ),
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pids([first])["a"], 111)
            self.assertEqual(backend.pane_pids([second])["b"], 222)
        run.assert_called_once()

    def test_invalidate_cache_forces_next_tmux_refresh(self):
        record = _record(id="a", tmux_name="sess_a")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        first = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\t%3\t111\n",
            stderr="",
        )
        second = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\t%3\t222\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", side_effect=[first, second]) as run:
            self.assertEqual(backend.pane_pids([record])["a"], 111)
            backend.invalidate_cache()
            self.assertEqual(backend.pane_pids([record])["a"], 222)
        self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()
