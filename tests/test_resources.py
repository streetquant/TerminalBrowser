import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from termdeck.resources import (
    ResourceSampler,
    _primary_codex_session_id,
    format_bytes,
    observe_recovery_command,
    observe_recovery_metadata,
    process_tree,
    process_tree_contains_codex,
    safe_codex_recovery_command,
)


class ResourceTests(unittest.TestCase):
    def test_codex_recovery_command_preserves_only_restart_safe_flags(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        self.assertEqual(
            safe_codex_recovery_command(["/usr/bin/codex", "--yolo", "--search"]),
            "codex --yolo --search",
        )
        self.assertEqual(
            safe_codex_recovery_command(
                ["/usr/bin/codex", "--yolo", "--search"],
                session_id=session_id,
            ),
            f"codex --yolo --search resume {session_id}",
        )
        self.assertEqual(
            safe_codex_recovery_command(
                ["/usr/bin/codex", "--yolo", "resume", session_id]
            ),
            f"codex --yolo resume {session_id}",
        )
        self.assertIsNone(safe_codex_recovery_command(["bash", "-lc", "codex --yolo"]))
        self.assertIsNone(safe_codex_recovery_command(["codex", "fix this private prompt"]))
        self.assertIsNone(safe_codex_recovery_command(["codex", "resume", "latest"]))
        self.assertIsNone(
            safe_codex_recovery_command(["codex", "--config", "api_key=secret"])
        )

    def test_primary_codex_session_id_excludes_subagent_rollouts(self):
        primary_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        subagent_id = "019f72ee-6244-7322-b922-660fcdcc91e7"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            fd_dir = proc / "123" / "fd"
            fd_dir.mkdir(parents=True)
            sessions = root / "sessions"
            sessions.mkdir()
            primary = sessions / f"rollout-primary-{primary_id}.jsonl"
            subagent = sessions / f"rollout-subagent-{subagent_id}.jsonl"
            primary.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": primary_id,
                            "cwd": "/workspace/project",
                            "originator": "codex-tui",
                            "source": "cli",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subagent.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": subagent_id,
                            "cwd": "/workspace/project",
                            "originator": "codex-tui",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {"parent_thread_id": primary_id}
                                }
                            },
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (fd_dir / "5").symlink_to(primary)
            (fd_dir / "6").symlink_to(subagent)

            with mock.patch("termdeck.resources._PROC", str(proc)), mock.patch(
                "termdeck.resources._process_cwd",
                return_value="/workspace/project",
            ):
                observed = _primary_codex_session_id(123)

        self.assertEqual(observed, primary_id)

    def test_recovery_observation_finds_codex_below_terminal_and_shell_wrappers(self):
        argv_by_pid = {
            100: ("/home/arch/.local/bin/kiro-cli-term",),
            101: ("/usr/bin/bash",),
            102: ("/usr/bin/codex", "--yolo"),
            103: ("/usr/bin/rg", "needle"),
        }
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101, 102, 103]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=lambda pid: argv_by_pid[pid],
        ), mock.patch(
            "termdeck.resources._parse_stat",
            return_value={"tty_nr": 86, "pgrp": 102, "tpgid": 102},
        ), mock.patch(
            "termdeck.resources._process_cwd",
            return_value="/8tb/projects/example",
        ), mock.patch(
            "termdeck.resources._primary_codex_session_id",
            side_effect=lambda pid: session_id if pid == 102 else None,
        ):
            observed, command, cwd = observe_recovery_metadata(100)

        self.assertTrue(observed)
        self.assertEqual(command, f"codex --yolo resume {session_id}")
        self.assertEqual(cwd, "/8tb/projects/example")

    def test_recovery_observation_accepts_exact_contained_codex_without_host_tty(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with mock.patch(
            "termdeck.resources.process_tree",
            return_value=[100, 101],
        ), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("bash",), ("/usr/bin/codex", "--yolo", "resume", session_id)],
        ), mock.patch(
            "termdeck.resources._parse_stat",
            return_value={"tty_nr": 0, "pgrp": 101, "tpgid": -1},
        ), mock.patch(
            "termdeck.resources._process_cwd",
            return_value="/workspace/project",
        ), mock.patch(
            "termdeck.resources._primary_codex_session_id",
            side_effect=lambda pid: session_id if pid == 101 else None,
        ):
            observed, command, cwd = observe_recovery_metadata(100)

        self.assertTrue(observed)
        self.assertEqual(command, f"codex --yolo resume {session_id}")
        self.assertEqual(cwd, "/workspace/project")

    def test_recovery_observation_prefers_native_exact_uuid_over_node_launcher(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        argv_by_pid = {
            100: ("bash",),
            101: ("/usr/bin/node", "/usr/bin/codex", "--yolo"),
            102: ("/usr/bin/codex", "--yolo"),
        }

        def argv(pid):
            if pid == 101:
                return ("/usr/bin/codex", "--yolo")
            return argv_by_pid[pid]

        with mock.patch(
            "termdeck.resources.process_tree",
            return_value=[100, 101, 102],
        ), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=argv,
        ), mock.patch(
            "termdeck.resources._parse_stat",
            return_value={"tty_nr": 86, "pgrp": 200, "tpgid": 200},
        ), mock.patch(
            "termdeck.resources._process_cwd",
            return_value="/workspace/project",
        ), mock.patch(
            "termdeck.resources._primary_codex_session_id",
            side_effect=lambda pid: session_id if pid == 102 else None,
        ):
            observed, command, cwd = observe_recovery_metadata(100)

        self.assertTrue(observed)
        self.assertEqual(command, f"codex --yolo resume {session_id}")
        self.assertEqual(cwd, "/workspace/project")

    def test_recovery_observation_distinguishes_idle_shell_from_unreadable_tree(self):
        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("kiro-cli-term",), ("bash",)],
        ):
            self.assertEqual(observe_recovery_command(100), (True, None))

        with mock.patch("termdeck.resources.process_tree", return_value=[]):
            self.assertEqual(observe_recovery_command(100), (False, None))

    def test_background_codex_is_not_marked_for_foreground_replay(self):
        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("bash",), ("codex", "--yolo")],
        ), mock.patch(
            "termdeck.resources._parse_stat",
            return_value={"tty_nr": 86, "pgrp": 101, "tpgid": 100},
        ), mock.patch(
            "termdeck.resources._primary_codex_session_id",
            return_value=None,
        ):
            self.assertEqual(observe_recovery_command(100), (True, None))

    def test_current_process_snapshot(self):
        sampler = ResourceSampler()
        first = sampler.snapshot(os.getpid())
        time.sleep(0.02)
        second = sampler.snapshot(os.getpid())
        self.assertTrue(second.exists)
        self.assertEqual(second.pid, os.getpid())
        self.assertGreaterEqual(second.memory_bytes, 1)
        self.assertGreaterEqual(second.process_count, 1)
        self.assertGreaterEqual(second.cpu_percent, 0.0)

    def test_process_tree_contains_self(self):
        self.assertIn(os.getpid(), process_tree(os.getpid()))

    def test_process_tree_includes_direct_children(self):
        children = [subprocess.Popen(["sleep", "2"]) for _ in range(3)]
        try:
            time.sleep(0.05)
            tree = set(process_tree(os.getpid()))
            self.assertIn(os.getpid(), tree)
            for child in children:
                self.assertIn(child.pid, tree)
        finally:
            for child in children:
                child.kill()
                child.wait(timeout=2)

    def test_process_tree_missing_pid_is_empty(self):
        self.assertEqual(process_tree(0), [])
        self.assertEqual(process_tree(999_999_999), [])

    def test_process_tree_is_fast_for_small_trees(self):
        # Guard against regressions that reintroduce full /proc scans per call.
        root = os.getpid()
        process_tree(root)  # warm
        started = time.perf_counter()
        for _ in range(100):
            process_tree(root)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # Full /proc scans are typically multi-ms each; fast walk should be << 50ms total.
        self.assertLess(elapsed_ms, 250.0, f"process_tree too slow: {elapsed_ms:.1f}ms for 100 calls")

    def test_process_tree_contains_codex_detects_native_and_node_launcher(self):
        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("/bin/bash",), ("/usr/bin/codex", "--yolo")],
        ):
            self.assertTrue(process_tree_contains_codex(100))

        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("/bin/bash",), ("/usr/bin/node", "/opt/openai/codex")],
        ):
            self.assertTrue(process_tree_contains_codex(100))

    def test_process_tree_contains_codex_proves_absence_only_when_fully_readable(self):
        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("/bin/bash",), ("/usr/bin/python", "worker.py")],
        ):
            self.assertFalse(process_tree_contains_codex(100))

        with mock.patch("termdeck.resources.process_tree", return_value=[100, 101]), mock.patch(
            "termdeck.resources._process_argv",
            side_effect=[("/bin/bash",), None],
        ):
            self.assertIsNone(process_tree_contains_codex(100))

        with mock.patch("termdeck.resources.process_tree", return_value=[]):
            self.assertIsNone(process_tree_contains_codex(100))


    def test_snapshot_uses_short_lived_cache(self):
        sampler = ResourceSampler(snapshot_cache_ttl=60)
        with mock.patch("termdeck.resources.process_tree", return_value=[123]) as tree,              mock.patch("termdeck.resources._parse_stat", return_value={"utime": 1, "stime": 1, "state": "S", "starttime": 1}),              mock.patch("termdeck.resources._rss_bytes", return_value=4096),              mock.patch.object(sampler, "_cached_total_jiffies", return_value=1000):
            first = sampler.snapshot(123)
            second = sampler.snapshot(123)
        self.assertIs(first, second)
        tree.assert_called_once_with(123)

    def test_snapshot_cache_invalidation_forces_resample(self):
        sampler = ResourceSampler(snapshot_cache_ttl=60)
        with mock.patch("termdeck.resources.process_tree", return_value=[123]) as tree,              mock.patch("termdeck.resources._parse_stat", return_value={"utime": 1, "stime": 1, "state": "S", "starttime": 1}),              mock.patch("termdeck.resources._rss_bytes", return_value=4096),              mock.patch.object(sampler, "_cached_total_jiffies", return_value=1000):
            first = sampler.snapshot(123)
            sampler.invalidate_cache(123)
            second = sampler.snapshot(123)
        self.assertIsNot(first, second)
        self.assertEqual(tree.call_count, 2)

    def test_two_thousand_roots_obey_round_robin_refresh_budget(self):
        sampler = ResourceSampler(snapshot_cache_ttl=0)
        roots = list(range(1, 2001))
        stat = {"utime": 1, "stime": 1, "state": "S", "starttime": 1}
        with mock.patch("termdeck.resources.process_tree", side_effect=lambda pid: [pid]) as tree, \
             mock.patch("termdeck.resources._parse_stat", return_value=stat), \
             mock.patch("termdeck.resources._rss_bytes", return_value=4096), \
             mock.patch.object(sampler, "_cached_total_jiffies", return_value=1000):
            first = sampler.snapshots(roots, refresh_limit=128)
            second = sampler.snapshots(roots, refresh_limit=128)

        self.assertEqual(len(first), 2000)
        self.assertEqual(len(second), 2000)
        self.assertEqual(tree.call_count, 256)
        self.assertEqual(sum(snapshot.state == "unknown" for snapshot in first), 1872)
        first_sampled = {snapshot.pid for snapshot in first if snapshot.state != "unknown"}
        second_sampled = {snapshot.pid for snapshot in second if snapshot.state != "unknown"}
        self.assertTrue(first_sampled.isdisjoint(second_sampled))

    def test_pid_churn_caches_are_bounded(self):
        sampler = ResourceSampler(snapshot_cache_ttl=60, max_cache_entries=4)
        stat = {"utime": 1, "stime": 1, "state": "S", "starttime": 1}
        with mock.patch("termdeck.resources.process_tree", side_effect=lambda pid: [pid]), \
             mock.patch("termdeck.resources._parse_stat", return_value=stat), \
             mock.patch("termdeck.resources._rss_bytes", return_value=4096), \
             mock.patch.object(sampler, "_cached_total_jiffies", return_value=1000):
            for pid in range(1, 11):
                sampler.snapshot(pid)

        self.assertLessEqual(len(sampler._snapshot_cache), 4)
        self.assertLessEqual(len(sampler._previous), 4)
        self.assertEqual(set(sampler._snapshot_cache), {7, 8, 9, 10})

    def test_format_bytes(self):
        self.assertEqual(format_bytes(1), "1 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")


if __name__ == "__main__":
    unittest.main()
