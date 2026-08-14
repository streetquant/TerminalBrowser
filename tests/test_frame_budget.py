"""Deterministic tests for frame-budget instrumentation without GTK."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from termdeck.frame_budget import FrameBudgetMonitor, Stall, hud_enabled, install_glib_probe


class FakeClock:
    """Yield planned values so timing edge cases need no real waiting."""

    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FrameBudgetMonitorTests(unittest.TestCase):
    """Exercise timing semantics directly, keeping performance tests repeatable."""

    def test_first_tick_sets_baseline_without_a_sample(self) -> None:
        monitor = FrameBudgetMonitor(clock=FakeClock([10.0]))

        self.assertIsNone(monitor.tick())
        report = monitor.report()
        self.assertEqual(1, report.ticks)
        self.assertEqual(0.0, report.p95_ms)
        self.assertEqual(0.0, report.worst_ms)

    def test_under_budget_gap_is_not_a_stall(self) -> None:
        monitor = FrameBudgetMonitor(budget_ms=8.0, clock=FakeClock([1.0, 1.007]))

        monitor.tick()

        self.assertIsNone(monitor.tick())
        report = monitor.report()
        self.assertEqual(0, report.stalls)
        self.assertAlmostEqual(7.0, report.worst_ms)

    def test_gap_equal_to_budget_is_not_a_stall(self) -> None:
        monitor = FrameBudgetMonitor(budget_ms=8.0, clock=FakeClock([0.0, 0.008]))

        monitor.tick()

        self.assertIsNone(monitor.tick())
        self.assertEqual(0, monitor.report().stalls)

    def test_over_budget_gap_returns_and_records_stall(self) -> None:
        monitor = FrameBudgetMonitor(budget_ms=8.0, clock=FakeClock([2.0, 2.011]))

        monitor.tick()
        stall = monitor.tick()

        self.assertIsInstance(stall, Stall)
        assert stall is not None
        self.assertEqual(2.0, stall.started_at)
        self.assertAlmostEqual(11.0, stall.duration_ms)
        self.assertEqual((stall,), monitor.report().recent_stalls)

    def test_p95_uses_all_gaps_not_just_stalls(self) -> None:
        monitor = FrameBudgetMonitor(
            budget_ms=50.0,
            clock=FakeClock([0.0, 0.001, 0.003, 0.006, 0.010, 0.110]),
        )

        for _ in range(6):
            monitor.tick()

        report = monitor.report()
        self.assertEqual(1, report.stalls)
        self.assertAlmostEqual(100.0, report.p95_ms)
        self.assertAlmostEqual(100.0, report.worst_ms)

    def test_p95_uses_nearest_rank(self) -> None:
        monitor = FrameBudgetMonitor(
            clock=FakeClock([0.0, 0.001, 0.003, 0.006, 0.010, 0.015]),
        )

        for _ in range(6):
            monitor.tick()

        self.assertAlmostEqual(5.0, monitor.report().p95_ms)

    def test_recent_stalls_are_bounded_to_configured_limit(self) -> None:
        monitor = FrameBudgetMonitor(
            budget_ms=1.0,
            max_recent=2,
            clock=FakeClock([0.0, 0.002, 0.005, 0.009]),
        )

        for _ in range(4):
            monitor.tick()

        recent = monitor.report().recent_stalls
        self.assertEqual(3, monitor.report().stalls)
        self.assertEqual(2, len(recent))
        self.assertAlmostEqual(3.0, recent[0].duration_ms)
        self.assertAlmostEqual(4.0, recent[1].duration_ms)

    def test_samples_are_bounded_for_percentile(self) -> None:
        monitor = FrameBudgetMonitor(
            clock=FakeClock([0.0, 0.001, 0.003, 0.006, 0.010]),
            max_samples=2,
        )

        for _ in range(5):
            monitor.tick()

        self.assertAlmostEqual(4.0, monitor.report().p95_ms)
        self.assertAlmostEqual(4.0, monitor.report().worst_ms)

    def test_reset_clears_history_and_baseline(self) -> None:
        monitor = FrameBudgetMonitor(budget_ms=1.0, clock=FakeClock([0.0, 0.003, 1.0]))
        monitor.tick()
        monitor.tick()

        monitor.reset()
        report = monitor.report()
        self.assertEqual(0, report.ticks)
        self.assertEqual(0, report.stalls)
        self.assertEqual(0.0, report.worst_ms)
        self.assertEqual(0.0, report.p95_ms)
        self.assertEqual((), report.recent_stalls)
        self.assertIsNone(monitor.tick())

    def test_zero_recent_capacity_is_supported(self) -> None:
        monitor = FrameBudgetMonitor(
            budget_ms=1.0, max_recent=0, clock=FakeClock([0.0, 0.003])
        )
        monitor.tick()

        self.assertIsNotNone(monitor.tick())
        self.assertEqual((), monitor.report().recent_stalls)

    def test_negative_capacity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FrameBudgetMonitor(max_recent=-1)
        with self.assertRaises(ValueError):
            FrameBudgetMonitor(max_samples=-1)

    def test_glib_installer_returns_none_without_gi(self) -> None:
        monitor = FrameBudgetMonitor(clock=FakeClock([0.0]))
        original_import = __import__

        def no_gi(name: str, *args: object, **kwargs: object) -> object:
            if name == "gi.repository":
                raise ImportError("GTK deliberately unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=no_gi):
            self.assertIsNone(install_glib_probe(monitor))


class HudEnabledTests(unittest.TestCase):
    """Verify explicitly supported environment spellings, not ambient settings."""

    def test_hud_is_disabled_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(hud_enabled())

    def test_hud_is_disabled_for_zero(self) -> None:
        with patch.dict(os.environ, {"T1000_PERF_HUD": "0"}, clear=True):
            self.assertFalse(hud_enabled())

    def test_hud_is_enabled_for_true_spellings(self) -> None:
        for value in ("1", "true", "yes", "TRUE", "Yes"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"T1000_PERF_HUD": value}, clear=True
            ):
                self.assertTrue(hud_enabled())
