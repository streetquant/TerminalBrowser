import os
import subprocess
import time
import unittest

from termdeck.resources import ResourceSampler, format_bytes, process_tree


class ResourceTests(unittest.TestCase):
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

    def test_format_bytes(self):
        self.assertEqual(format_bytes(1), "1 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")


if __name__ == "__main__":
    unittest.main()
