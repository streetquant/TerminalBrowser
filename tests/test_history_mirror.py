"""Deterministic unittest coverage for the local history mirror."""

from __future__ import annotations

import unittest

from termdeck.history_mirror import HistoryMirror, MirrorLine, PrefetchRequest, Provenance


class HistoryMirrorTests(unittest.TestCase):
    def test_seed_then_live_feed_grows_absolute_range(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["old-0", "old-1"], start_line=8, total_history_lines=10)

        self.assertEqual(mirror.feed_output("live-10\nlive-11\n"), 12)
        self.assertEqual(mirror.mirrored_range().start_line, 8)
        self.assertEqual(mirror.mirrored_range().end_line, 12)
        self.assertEqual(mirror.line_at(8), MirrorLine("old-0", Provenance.BACKFILL))
        self.assertEqual(mirror.line_at(10), MirrorLine("live-10", Provenance.LIVE_OUTPUT))
        self.assertEqual(mirror.known_backend_extent(), 12)

    def test_partial_line_is_buffered_across_live_feeds(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill([], start_line=5, total_history_lines=5)

        self.assertEqual(mirror.feed_output("par"), 5)
        self.assertEqual(mirror.mirrored_range().end_line, 5)
        self.assertEqual(mirror.feed_output("tial\n"), 6)
        self.assertEqual(mirror.line_at(5), MirrorLine("partial", Provenance.LIVE_OUTPUT))

    def test_carriage_return_progress_replaces_pending_partial(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill([], start_line=0, total_history_lines=0)

        mirror.feed_output("20%")
        mirror.feed_output("\r100%")
        mirror.feed_output("\n")

        self.assertEqual(mirror.line_at(0), MirrorLine("100%", Provenance.LIVE_OUTPUT))

    def test_crlf_completes_one_line(self) -> None:
        mirror = HistoryMirror()
        mirror.feed_output("windows\r\nnext\n")

        self.assertEqual(
            mirror.lines(0, 2),
            [
                MirrorLine("windows", Provenance.LIVE_OUTPUT),
                MirrorLine("next", Provenance.LIVE_OUTPUT),
            ],
        )

    def test_ring_eviction_advances_start_without_gap(self) -> None:
        mirror = HistoryMirror(max_lines=3)
        mirror.seed_backfill(["a", "b", "c"], start_line=10, total_history_lines=13)

        mirror.feed_output("d\ne\n")

        self.assertEqual(mirror.mirrored_range().start_line, 12)
        self.assertEqual(mirror.mirrored_range().end_line, 15)
        self.assertTrue(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.lines(12, 3), [
            MirrorLine("c", Provenance.BACKFILL),
            MirrorLine("d", Provenance.LIVE_OUTPUT),
            MirrorLine("e", Provenance.LIVE_OUTPUT),
        ])
        self.assertEqual(mirror.stats()["evicted"], 2)
        self.assertEqual(mirror.stats()["gaps"], 0)

    def test_prefetch_only_fires_near_mirror_start(self) -> None:
        mirror = HistoryMirror(page_size=4, prefetch_margin=2)
        mirror.seed_backfill(["8", "9"], start_line=8, total_history_lines=10)

        self.assertIsNone(mirror.needs_prefetch(11))
        self.assertEqual(mirror.needs_prefetch(10), PrefetchRequest(4, 4))

    def test_prefetch_respects_pending_request(self) -> None:
        mirror = HistoryMirror(page_size=4, prefetch_margin=3)
        mirror.seed_backfill(["8", "9"], start_line=8, total_history_lines=10)

        self.assertIsNotNone(mirror.needs_prefetch(8))
        self.assertIsNone(mirror.needs_prefetch(8))
        self.assertEqual(mirror.stats()["pending_prefetches"], 1)

    def test_prefetch_not_requested_when_start_is_zero(self) -> None:
        mirror = HistoryMirror(prefetch_margin=999)
        mirror.seed_backfill(["zero"], start_line=0, total_history_lines=1)

        self.assertIsNone(mirror.needs_prefetch(0))

    def test_abutting_prefetch_stitches_before_start(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["c", "d"], start_line=2, total_history_lines=4)
        request = mirror.needs_prefetch(2)
        self.assertEqual(request, PrefetchRequest(0, 2))

        self.assertTrue(mirror.apply_prefetch(["a", "b"], start_line=0))
        self.assertEqual(mirror.mirrored_range().start_line, 0)
        self.assertEqual(mirror.mirrored_range().end_line, 4)
        self.assertEqual(mirror.lines(0, 4), [
            MirrorLine("a", Provenance.PREFETCH),
            MirrorLine("b", Provenance.PREFETCH),
            MirrorLine("c", Provenance.BACKFILL),
            MirrorLine("d", Provenance.BACKFILL),
        ])
        self.assertEqual(mirror.stats()["pending_prefetches"], 0)

    def test_non_abutting_prefetch_marks_gap(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["five", "six"], start_line=5, total_history_lines=7)

        self.assertFalse(mirror.apply_prefetch(["zero", "one"], start_line=0))
        self.assertFalse(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.stats()["gaps"], 1)

    def test_reapplying_already_mirrored_prefetch_is_idempotent(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["two", "three"], start_line=2, total_history_lines=4)
        self.assertTrue(mirror.apply_prefetch(["zero", "one"], start_line=0))

        self.assertTrue(mirror.apply_prefetch(["zero", "one"], start_line=0))
        self.assertTrue(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.lines(0, 4), [
            MirrorLine("zero", Provenance.PREFETCH),
            MirrorLine("one", Provenance.PREFETCH),
            MirrorLine("two", Provenance.BACKFILL),
            MirrorLine("three", Provenance.BACKFILL),
        ])

    def test_note_gap_then_reseed_restores_contiguity(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["a"], start_line=0, total_history_lines=1)
        mirror.note_gap()
        self.assertFalse(mirror.mirrored_range().contiguous)

        mirror.seed_backfill(["b"], start_line=20, total_history_lines=21)
        self.assertTrue(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.mirrored_range().start_line, 20)
        self.assertEqual(mirror.stats()["gaps"], 1)

    def test_checksum_mismatch_marks_gap_and_match_does_not(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["correct"], start_line=4, total_history_lines=5)

        self.assertTrue(mirror.checksum_audit(4, "correct"))
        self.assertTrue(mirror.mirrored_range().contiguous)
        self.assertFalse(mirror.checksum_audit(4, "incorrect"))
        self.assertFalse(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.stats()["gaps"], 1)

    def test_line_access_and_bulk_lines_clip_to_held_interval(self) -> None:
        mirror = HistoryMirror()
        mirror.seed_backfill(["a", "b", "c"], start_line=10, total_history_lines=13)

        self.assertIsNone(mirror.line_at(9))
        self.assertIsNone(mirror.line_at(13))
        self.assertEqual(
            mirror.lines(8, 10),
            [
                MirrorLine("a", Provenance.BACKFILL),
                MirrorLine("b", Provenance.BACKFILL),
                MirrorLine("c", Provenance.BACKFILL),
            ],
        )
        self.assertEqual(mirror.lines(10, 0), [])
        self.assertEqual(mirror.lines(100, 2), [])

    def test_large_seed_keeps_newest_contiguous_suffix(self) -> None:
        mirror = HistoryMirror(max_lines=2)
        mirror.seed_backfill(["a", "b", "c"], start_line=10, total_history_lines=13)

        self.assertEqual(mirror.mirrored_range().start_line, 11)
        self.assertEqual(mirror.mirrored_range().end_line, 13)
        self.assertTrue(mirror.mirrored_range().contiguous)
        self.assertEqual(mirror.stats()["evicted"], 1)

    def test_invalid_configuration_and_coordinates_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HistoryMirror(max_lines=0)
        with self.assertRaises(ValueError):
            HistoryMirror(page_size=0)
        with self.assertRaises(ValueError):
            HistoryMirror(prefetch_margin=-1)

        mirror = HistoryMirror()
        with self.assertRaises(ValueError):
            mirror.seed_backfill([], start_line=-1, total_history_lines=0)
        with self.assertRaises(ValueError):
            mirror.apply_prefetch([], start_line=-1)


if __name__ == "__main__":
    unittest.main()
