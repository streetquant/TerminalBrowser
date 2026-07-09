import subprocess
import unittest
from unittest import mock

from termdeck.models import SessionRecord
from termdeck.tmux_backend import TmuxBackend


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
