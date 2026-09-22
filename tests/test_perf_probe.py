"""The performance job must execute measurements and fail on broken probes."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("termdeck_perf_probe", ROOT / "scripts/perf_probe.py")
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class PerformanceProbeTests(unittest.TestCase):
    def run_main(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = PROBE.main(args)
        return code, json.loads(output.getvalue())

    def test_ci_arguments_execute_the_requested_workload(self):
        code, result = self.run_main(["--sessions", "36", "--iterations", "5", "--assert-thresholds"])
        # Wall-clock speed varies by host; this test asserts the contract, not
        # a speed result. CI is responsible for enforcing the actual budgets.
        self.assertIn(code, (0, 1))
        self.assertEqual(result["workload"], {"sessions": 36, "iterations": 5})
        self.assertTrue(result["thresholds_enforced"])
        self.assertEqual(result["probes"]["snapshot_build"]["n"], 5)
        self.assertEqual(result["ok"], code == 0)

    def test_session_count_reaches_snapshot_builder(self):
        from termdeck.task_snapshot import build_task_snapshot

        with patch("termdeck.task_snapshot.build_task_snapshot", wraps=build_task_snapshot) as build:
            PROBE.run_probes(iterations=2, sessions=7)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(len(build.call_args.args[0]), 7)

    def test_probe_error_fails_even_without_threshold_flag(self):
        with patch.object(PROBE, "_snapshot_build_probe", return_value={"error": "broken snapshot"}):
            code, result = self.run_main(["--iterations", "1"])
        self.assertEqual(code, 1)
        self.assertIn("snapshot_build: broken snapshot", result["failures"])

    def test_threshold_breach_fails_only_when_requested(self):
        with patch.object(PROBE, "_snapshot_build_probe", return_value={"p95_ms": 999.0, "n": 1}):
            self.assertEqual(self.run_main(["--iterations", "1"])[0], 0)
            code, result = self.run_main(["--iterations", "1", "--assert-thresholds"])
        self.assertEqual(code, 1)
        self.assertIn("exceeds", result["failures"][0])

    def test_invalid_measurements_cannot_pass(self):
        for invalid in ({}, {"p95_ms": float("nan"), "n": 1}, {"p95_ms": -1, "n": 1}, {"p95_ms": 0, "n": 0}):
            with self.subTest(invalid=invalid), patch.object(PROBE, "_snapshot_build_probe", return_value=invalid):
                self.assertEqual(self.run_main(["--iterations", "1"])[0], 1)

    def test_nonpositive_workload_is_rejected(self):
        for args in (["--sessions", "0"], ["--iterations", "0"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                PROBE._parse_args(args)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
