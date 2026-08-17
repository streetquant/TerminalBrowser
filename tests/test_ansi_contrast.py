'''Stream-level contrast floor: programs that pick their own ink stay readable.

Regression suite for the 'black text remains' bug. ``ensure_legible_theme``
governs only the sixteen VTE palette slots, so a program that emits an indexed
(``38;5;n``) or truecolour (``38;2;r;g;b``) foreground bypasses the theme
entirely: Codex does exactly that when it mis-detects TermDeck's dark pane as a
light terminal and styles headings in near-black ink. Two defences must hold.
``SgrContrastFilter`` lifts those foregrounds in the byte stream on the way into
VTE, and ``merge_vte_attach_environment`` declares a dark ``COLORFGBG`` so a
well-behaved child never makes the wrong choice in the first place.
'''

from __future__ import annotations

import ast
import random
import re
import unittest
from pathlib import Path

from termdeck.ansi_contrast import (
    MAX_PENDING_BYTES,
    MAX_SGR_BYTES,
    MAX_SGR_PARAMETER_DIGITS,
    SgrContrastFilter,
    parse_sgr_groups,
    scan_sequence,
)
from termdeck.models import TerminalTheme
from termdeck.ui_theme import (
    TERMINAL_MIN_STREAM_CONTRAST,
    contrast_ratio,
    ensure_legible_theme,
    xterm_indexed_color,
)
from termdeck.vte_stack import DARK_COLORFGBG, merge_vte_attach_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GTK_APP = PROJECT_ROOT / 'termdeck' / 'gtk_app.py'

#: String Terminator: ESC followed by a literal backslash.
_ST = b'\x1b\x5c'
_TRUECOLOUR_FG = re.compile(b'38;2;([0-9]+);([0-9]+);([0-9]+)')


def _emitted_foreground(payload: bytes) -> str:
    '''Return the last truecolour foreground the filter wrote, as #rrggbb.'''

    matches = _TRUECOLOUR_FG.findall(payload)
    if not matches:
        raise AssertionError('no truecolour foreground in %r' % (payload,))
    red, green, blue = (int(value) for value in matches[-1])
    return '#%02x%02x%02x' % (red, green, blue)


def _default_background() -> str:
    return ensure_legible_theme(TerminalTheme()).background_color


def _state_signature(filter_: SgrContrastFilter) -> tuple[object, ...]:
    """Comparable stream/parser state used by whole-vs-chunked properties."""

    return (
        filter_._pending,
        filter_._opaque_kind,
        filter_._opaque_string_esc,
        filter_._fg,
        filter_._bg,
        filter_._reverse,
        filter_._state_tainted,
    )


class StreamConstantTests(unittest.TestCase):
    def test_stream_floor_matches_body_text_floor(self):
        '''Self-chosen colours are body text, not large text: 4.5, not 3.0.'''

        self.assertEqual(TERMINAL_MIN_STREAM_CONTRAST, 4.5)


class SequenceScannerTests(unittest.TestCase):
    def test_sgr_is_distinguished_from_other_csi(self):
        self.assertEqual(scan_sequence(b'\x1b[31m', 0), (5, 'sgr'))
        self.assertEqual(scan_sequence(b'\x1b[2J', 0), (4, 'other'))
        self.assertEqual(scan_sequence(b'\x1b[10;20H', 0), (8, 'other'))

    def test_osc_string_terminates_on_bel_or_st(self):
        self.assertEqual(scan_sequence(b'\x1b]0;title\x07', 0), (10, 'other'))
        self.assertEqual(scan_sequence(b'\x1b]0;title' + _ST, 0), (11, 'other'))

    def test_truncated_sequences_report_incomplete(self):
        self.assertEqual(scan_sequence(b'\x1b', 0), (None, None))
        self.assertEqual(scan_sequence(b'\x1b[38;2;1', 0), (None, None))
        self.assertEqual(scan_sequence(b'\x1b]0;partial', 0), (None, None))

    def test_extended_colour_specs_stay_one_group(self):
        self.assertEqual(
            parse_sgr_groups(b'1;38;2;10;20;30;48;5;236'),
            [['1'], ['38', '2', '10', '20', '30'], ['48', '5', '236']],
        )

    def test_colon_extended_colour_specs_stay_one_group(self):
        self.assertEqual(
            parse_sgr_groups(b'1;4:3;38:2::10:20:30;48:5:236'),
            [
                ['1'],
                ['4', '3'],
                ['38', '2', '', '10', '20', '30'],
                ['48', '5', '236'],
            ],
        )

    def test_unsupported_bodies_are_rejected_rather_than_guessed(self):
        self.assertIsNone(parse_sgr_groups(b'38;9;1'))
        self.assertIsNone(parse_sgr_groups(b'38;2;1;2'))
        self.assertIsNone(parse_sgr_groups(b'1;?'))

    def test_oversized_complete_sgr_is_classified_without_integer_parsing(self):
        sequence = b'\x1b[' + (b'9' * MAX_SGR_BYTES) + b'm'
        self.assertEqual(scan_sequence(sequence, 0), (len(sequence), 'sgr-oversized'))

    def test_nested_escape_cancels_csi_without_swallowing_replacement(self):
        payload = b'\x1b[123\x1b[31m'
        nested = payload.index(b'\x1b', 1)
        self.assertEqual(scan_sequence(payload, 0), (nested, 'other'))
        self.assertEqual(scan_sequence(payload, nested), (len(payload), 'sgr'))

    def test_giant_decimal_token_is_rejected_without_value_error(self):
        body = b'38;2;' + (b'9' * 5000) + b';0;0'
        self.assertIsNone(parse_sgr_groups(body))
        self.assertEqual(MAX_SGR_PARAMETER_DIGITS, MAX_SGR_BYTES)


class ForegroundLiftTests(unittest.TestCase):
    def setUp(self):
        self.background = _default_background()
        self.filter = SgrContrastFilter(TerminalTheme())

    def assert_legible(self, payload: bytes) -> None:
        emitted = _emitted_foreground(payload)
        self.assertGreaterEqual(
            contrast_ratio(emitted, self.background),
            TERMINAL_MIN_STREAM_CONTRAST,
            '%s on %s is still unreadable' % (emitted, self.background),
        )

    def test_near_black_truecolour_heading_is_lifted(self):
        '''The exact shape of the reported bug: Codex ink at rgb(16,16,16).'''

        out = self.filter.process(b'\x1b[1;38;2;16;16;16mHeading\x1b[0m')
        self.assertIn(b'Heading', out)
        self.assert_legible(out)

    def test_near_black_colon_truecolour_heading_is_lifted(self):
        '''VTE accepts ISO colon subparameters; they must not bypass the floor.'''

        out = self.filter.process(b'\x1b[1;38:2::16:16:16mHeading\x1b[0m')
        self.assertIn(b'Heading', out)
        self.assertNotIn(b'38:2::16:16:16', out)
        self.assert_legible(out)

    def test_colon_truecolour_with_zero_colourspace_is_lifted(self):
        out = self.filter.process(b'\x1b[38:2:0:16:16:16mzero-space')
        self.assert_legible(out)

    def test_pure_black_truecolour_is_lifted(self):
        self.assert_legible(self.filter.process(b'\x1b[38;2;0;0;0mblack'))

    def test_dark_xterm256_grey_is_lifted(self):
        dark_grey = xterm_indexed_color(236)
        self.assertIsNotNone(dark_grey)
        self.assertLess(
            contrast_ratio(dark_grey, self.background),
            TERMINAL_MIN_STREAM_CONTRAST,
            'fixture no longer exercises the lift path',
        )
        self.assert_legible(self.filter.process(b'\x1b[38;5;236mgrey'))

    def test_dark_colon_xterm256_grey_is_lifted(self):
        self.assert_legible(self.filter.process(b'\x1b[38:5:236mgrey'))

    def test_unrelated_colon_style_survives_foreground_rewrite(self):
        out = self.filter.process(b'\x1b[4:3;38:2::16:16:16mstyled')
        self.assertIn(b'4:3', out)
        self.assert_legible(out)

    def test_bold_and_background_attributes_survive_the_rewrite(self):
        out = self.filter.process(b'\x1b[1;3;38;2;16;16;16;48;5;236mstyled')
        head = out[: out.index(b'm')]
        groups = head[2:].split(b';')
        self.assertIn(b'1', groups)
        self.assertIn(b'3', groups)
        self.assertIn(b'48;5;236', head)
        self.assertIn(b'styled', out)
        self.assert_legible(out)

    def test_legible_foreground_passes_through_byte_identical(self):
        payload = b'\x1b[38;2;223;230;242mbright\x1b[0m'
        self.assertEqual(self.filter.process(payload), payload)

    def test_palette_slots_are_left_to_the_theme_guard(self):
        '''SGR 30-37 resolve through the already-legible palette.'''

        payload = b'\x1b[30mansi black\x1b[39m plain'
        self.assertEqual(self.filter.process(payload), payload)

    def test_reverse_active_foreground_is_not_rewritten(self):
        payload = b'\x1b[7;38;2;16;16;16mreversed'
        self.assertEqual(self.filter.process(payload), payload)

    def test_reverse_reset_lifts_the_foreground_before_normal_text(self):
        self.filter.process(b'\x1b[7;38;2;16;16;16mreversed')
        restored = self.filter.process(b'\x1b[27mnormal')
        self.assertIn(b'27', restored)
        self.assertIn(b'normal', restored)
        self.assert_legible(restored)

    def test_dark_ink_on_an_explicit_light_background_is_kept(self):
        '''A program painting its own light background is self-consistent.'''

        payload = b'\x1b[48;2;255;255;255;38;2;16;16;16mreceipt'
        self.assertEqual(self.filter.process(payload), payload)

    def test_colon_dark_ink_on_an_explicit_light_background_is_kept(self):
        payload = b'\x1b[48:2::255:255:255;38:2::16:16:16mreceipt'
        self.assertEqual(self.filter.process(payload), payload)

    def test_unknown_colon_colourspace_is_preserved_fail_open(self):
        payload = b'\x1b[38:2:1:16:16:16munknown-space'
        self.assertEqual(self.filter.process(payload), payload)

    def test_unknown_colon_indexed_tail_is_preserved_fail_open(self):
        payload = b'\x1b[38:5:236:7munknown-indexed-tail'
        self.assertEqual(self.filter.process(payload), payload)

    def test_invalid_rgb_is_a_vte_noop_and_keeps_previous_foreground(self):
        self.filter.process(b'\x1b[7;38;2;16;16;16m')
        invalid = b'\x1b[38;2;256;0;0m'
        restored = self.filter.process(invalid + b'\x1b[27mnormal')
        self.assertIn(invalid, restored)
        self.assert_legible(restored)

    def test_invalid_colon_rgb_is_a_vte_noop(self):
        self.filter.process(b'\x1b[7;38:2::16:16:16m')
        invalid = b'\x1b[38:2::256:0:0m'
        restored = self.filter.process(invalid + b'\x1b[27mnormal')
        self.assertIn(invalid, restored)
        self.assert_legible(restored)

    def test_invalid_indexed_colour_is_a_vte_noop(self):
        self.filter.process(b'\x1b[7;38;5;236m')
        invalid = b'\x1b[38;5;256m'
        restored = self.filter.process(invalid + b'\x1b[27mnormal')
        self.assertIn(invalid, restored)
        self.assert_legible(restored)

    def test_invalid_background_preserves_previous_light_background(self):
        self.filter.process(b'\x1b[48;2;255;255;255m')
        payload = b'\x1b[48;2;256;0;0m\x1b[38;2;16;16;16mreceipt'
        self.assertEqual(self.filter.process(payload), payload)

    def test_rgb_and_indexed_boundaries_accept_255_but_reject_256(self):
        valid_rgb = b'\x1b[38;2;255;0;255mvalid-rgb'
        valid_indexed = b'\x1b[38;5;255mvalid-indexed'
        self.assertEqual(self.filter.process(valid_rgb), valid_rgb)
        self.filter.reset()
        self.assertEqual(self.filter.process(valid_indexed), valid_indexed)

        for invalid in (
            b'\x1b[38;2;256;0;0m',
            b'\x1b[38:2::256:0:0m',
            b'\x1b[38;5;256m',
            b'\x1b[38:5:256m',
        ):
            self.filter.reset()
            self.assertEqual(self.filter.process(invalid), invalid)
            self.assertFalse(self.filter._state_tainted)

    def test_long_bounded_numeric_colour_is_invalid_noop_not_malformed(self):
        huge_channel = b'9' * 200
        invalid = b'\x1b[38;2;' + huge_channel + b';0;0m'
        self.filter.process(b'\x1b[7;38;2;16;16;16m')
        restored = self.filter.process(invalid + b'\x1b[27mnormal')
        self.assertIn(invalid, restored)
        self.assertFalse(self.filter._state_tainted)
        self.assert_legible(restored)

    def test_long_bounded_unknown_code_does_not_poison_later_valid_group(self):
        unknown = b'9' * 200
        payload = b'\x1b[' + unknown + b';38;2;16;16;16mnormal'
        out = self.filter.process(payload)
        self.assertFalse(self.filter._state_tainted)
        self.assertIn(unknown, out)
        self.assert_legible(out)


class StreamIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.filter = SgrContrastFilter(TerminalTheme())

    def assert_lifted(self, payload: bytes) -> None:
        self.assertGreaterEqual(
            contrast_ratio(_emitted_foreground(payload), _default_background()),
            TERMINAL_MIN_STREAM_CONTRAST,
        )

    def test_plain_text_is_untouched(self):
        payload = b'no escapes here\r\n\tjust bytes\x00\xff'
        self.assertEqual(self.filter.process(payload), payload)

    def test_cursor_motion_and_osc_pass_through_byte_identical(self):
        payload = (
            b'\x1b[2J\x1b[10;20H\x1b]0;TermDeck\x07'
            + b'\x1b]52;c;ZGF0YQ==' + _ST
            + b'\x1bPq~~' + _ST
            + b'text'
        )
        self.assertEqual(self.filter.process(payload), payload)

    def test_sequence_split_across_reads_is_buffered_not_mangled(self):
        whole = b'\x1b[1;38;2;16;16;16mHeading\x1b[0mtail'
        expected = SgrContrastFilter(TerminalTheme()).process(whole)
        pieces = [whole[:4], whole[4:9], whole[9:17], whole[17:]]
        streamed = b''.join(self.filter.process(piece) for piece in pieces)
        self.assertEqual(streamed, expected)
        self.assert_lifted(streamed)

    def test_split_osc_string_is_reassembled(self):
        first = self.filter.process(b'\x1b]0;Term')
        second = self.filter.process(b'Deck\x07done')
        self.assertEqual(first + second, b'\x1b]0;TermDeck\x07done')

    def test_partial_tail_is_withheld_then_completed(self):
        self.assertEqual(self.filter.process(b'abc\x1b[38;2;16;16'), b'abc')
        self.assert_lifted(self.filter.process(b';16mx'))

    def test_runaway_partial_sequence_is_flushed_verbatim(self):
        '''A malformed escape must not stall pane output indefinitely.'''

        runaway = b'\x1b[' + b'1;' * MAX_PENDING_BYTES
        self.assertEqual(self.filter.process(runaway), runaway)
        self.assertEqual(self.filter._opaque_kind, 'csi')

    def test_complete_giant_sgr_is_total_byte_preserving_and_taints_state(self):
        giant = b'\x1b[' + (b'9' * 5000) + b'm'
        self.assertEqual(self.filter.process(giant), giant)
        self.assertTrue(self.filter._state_tainted)
        self.assertLessEqual(len(self.filter._pending), MAX_PENDING_BYTES)

    def test_tainted_state_recovers_only_at_full_reset(self):
        malformed = b'\x1b[38;9;1m'
        self.assertEqual(self.filter.process(malformed), malformed)
        dark = b'\x1b[38;2;16;16;16mdark'
        self.assertEqual(self.filter.process(dark), dark)
        recovered = self.filter.process(b'\x1b[0;38;2;16;16;16mnormal')
        self.assertFalse(self.filter._state_tainted)
        self.assert_lifted(recovered)

    def test_oversized_csi_has_identical_whole_and_multi_chunk_state(self):
        payload = (
            b'\x1b[7;38;2;16;16;16m'
            + b'\x1b['
            + (b'9' * (MAX_PENDING_BYTES + 64))
            + b'm'
            + b'\x1b[27mnormal'
        )
        whole_filter = SgrContrastFilter(TerminalTheme())
        whole = whole_filter.process(payload)
        split_filter = SgrContrastFilter(TerminalTheme())
        cuts = (17, 1000, MAX_PENDING_BYTES + 11, len(payload) - 7)
        start = 0
        pieces = []
        for end in cuts + (len(payload),):
            pieces.append(split_filter.process(payload[start:end]))
            start = end
        self.assertEqual(b''.join(pieces), whole)
        self.assertEqual(_state_signature(split_filter), _state_signature(whole_filter))

    def test_long_osc_keeps_embedded_sgr_opaque_after_pending_cap(self):
        embedded = b'\x1b[0;38;2;16;16;16m'
        payload = (
            b'\x1b]0;'
            + (b'x' * (MAX_PENDING_BYTES + 64))
            + embedded
            + b'\x07'
            + b'\x1b[38;2;16;16;16mvisible'
        )
        whole_filter = SgrContrastFilter(TerminalTheme())
        whole = whole_filter.process(payload)
        split_filter = SgrContrastFilter(TerminalTheme())
        split = b''.join(
            split_filter.process(part)
            for part in (
                payload[:1000],
                payload[1000 : MAX_PENDING_BYTES + 20],
                payload[MAX_PENDING_BYTES + 20 : -12],
                payload[-12:],
            )
        )
        self.assertEqual(split, whole)
        self.assertIn(embedded, split)
        self.assertEqual(_state_signature(split_filter), _state_signature(whole_filter))

    def test_long_control_string_recognizes_split_st_terminator(self):
        body = b'\x1bP' + (b'q' * (MAX_PENDING_BYTES + 32))
        suffix = b'\x1b\\\x1b[38;2;16;16;16mvisible'
        whole_filter = SgrContrastFilter(TerminalTheme())
        whole = whole_filter.process(body + suffix)
        split_filter = SgrContrastFilter(TerminalTheme())
        split = (
            split_filter.process(body)
            + split_filter.process(b'\x1b')
            + split_filter.process(suffix[1:])
        )
        self.assertEqual(split, whole)
        self.assert_lifted(split)

    def test_nested_escape_cancels_incomplete_csi_and_rewrites_new_sgr(self):
        payload = b'\x1b[123\x1b[38;2;16;16;16mvisible'
        out = self.filter.process(payload)
        self.assertTrue(out.startswith(b'\x1b[123'))
        self.assert_lifted(out)

    def test_cache_and_pending_storage_remain_bounded(self):
        for index in range(700):
            fg = (index % 256, (index // 256) % 256, (index // 65536) % 256)
            bg = ((index * 3) % 32, (index * 5) % 32, (index * 7) % 32)
            payload = (
                b'\x1b[0;48;2;%d;%d;%d;38;2;%d;%d;%dmX'
                % (*bg, *fg)
            )
            self.filter.process(payload)
        self.assertLessEqual(len(self.filter._cache), 512)
        self.assertLessEqual(len(self.filter._pending), MAX_PENDING_BYTES)

    def test_reset_drops_pending_bytes_and_sgr_state(self):
        self.filter.process(b'\x1b[38;2;16;16')
        self.filter.reset()
        self.assertEqual(self.filter.process(b'plain'), b'plain')

    def test_set_theme_retargets_the_background_reference(self):
        light = TerminalTheme(background_color='#ffffff', foreground_color='#101010')
        self.filter.set_theme(light)
        legible_light = ensure_legible_theme(light)
        out = self.filter.process(b'\x1b[38;2;250;250;250mfaint')
        self.assertGreaterEqual(
            contrast_ratio(_emitted_foreground(out), legible_light.background_color),
            TERMINAL_MIN_STREAM_CONTRAST,
        )


class DeterministicPropertyTests(unittest.TestCase):
    def test_arbitrary_multi_part_chunking_and_idempotence(self):
        rng = random.Random(0x5444310003)
        giant_sgr = b'\x1b[' + (b'9' * 5000) + b'm'
        long_osc = b'\x1b]0;' + (b'x' * 4200) + b'\x07'
        fragments = (
            b'plain',
            b'\r\n',
            b'\x00\xff',
            b'\x1b[0m',
            b'\x1b[7m',
            b'\x1b[27m',
            b'\x1b[38;2;16;16;16m',
            b'\x1b[38:2::16:16:16m',
            b'\x1b[38;2;255;255;255m',
            b'\x1b[38;2;256;0;0m',
            b'\x1b[38;5;255m',
            b'\x1b[38;5;256m',
            b'\x1b[48;2;255;255;255m',
            b'\x1b[38:2:1:16:16:16m',
            b'\x1b[4:3m',
            b'\x1b[?25l',
            long_osc,
            giant_sgr,
        )
        for case in range(1000):
            payload = b''.join(
                rng.choice(fragments) for _ in range(rng.randrange(1, 8))
            )
            whole_filter = SgrContrastFilter(TerminalTheme())
            whole = whole_filter.process(payload)

            split_filter = SgrContrastFilter(TerminalTheme())
            cuts = sorted(
                {0, len(payload)}
                | {
                    rng.randrange(len(payload) + 1)
                    for _ in range(rng.randrange(1, 10))
                }
            )
            split = b''.join(
                split_filter.process(payload[start:end])
                for start, end in zip(cuts, cuts[1:])
            )
            self.assertEqual(split, whole, f'chunk mismatch in case {case}')
            self.assertEqual(
                _state_signature(split_filter),
                _state_signature(whole_filter),
                f'state mismatch in case {case}',
            )

            replay = SgrContrastFilter(TerminalTheme()).process(whole)
            self.assertEqual(replay, whole, f'non-idempotent case {case}')

    def test_every_byte_value_is_total_at_every_single_split(self):
        payload = bytes(range(256)) + b'\x1b[38;2;16;16;16mvisible'
        expected_filter = SgrContrastFilter(TerminalTheme())
        expected = expected_filter.process(payload)
        for split_at in range(len(payload) + 1):
            filter_ = SgrContrastFilter(TerminalTheme())
            observed = filter_.process(payload[:split_at]) + filter_.process(
                payload[split_at:]
            )
            self.assertEqual(observed, expected, split_at)
            self.assertEqual(_state_signature(filter_), _state_signature(expected_filter))


class ChildEnvironmentTests(unittest.TestCase):
    def test_dark_colorfgbg_is_declared_for_the_child(self):
        environment = merge_vte_attach_environment({})
        self.assertEqual(environment['COLORFGBG'], DARK_COLORFGBG)

    def test_inherited_light_colorfgbg_is_overridden(self):
        environment = merge_vte_attach_environment({'COLORFGBG': '0;15'})
        self.assertEqual(environment['COLORFGBG'], DARK_COLORFGBG)

    def test_dark_value_names_a_light_foreground_on_a_dark_background(self):
        foreground, background = DARK_COLORFGBG.split(';')
        self.assertEqual(foreground, '15')
        self.assertEqual(background, '0')

    def test_unrelated_base_environment_is_preserved(self):
        environment = merge_vte_attach_environment({'KEEP_ME': '1'})
        self.assertEqual(environment['KEEP_ME'], '1')
        self.assertEqual(environment['TERM_PROGRAM'], 'TermDeck')


class GtkIntegrationStaticTests(unittest.TestCase):
    '''gtk_app.py needs a live gi/Vte stack, so assert on its source instead.'''

    @classmethod
    def setUpClass(cls):
        cls.source = GTK_APP.read_text(encoding='utf-8')

    def test_filter_is_imported_from_the_owning_module(self):
        tree = ast.parse(self.source)
        imported = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == 'ansi_contrast'
            for alias in node.names
        }
        self.assertIn('SgrContrastFilter', imported)

    def test_filter_is_constructed_with_the_pane_theme(self):
        self.assertIn('self._contrast_filter = SgrContrastFilter(theme)', self.source)

    def test_theme_changes_retarget_the_filter_before_vte_is_restyled(self):
        anchor = self.source.index('def apply_window_theme(self, theme: TerminalTheme')
        set_theme = self.source.index('self._contrast_filter.set_theme(theme)', anchor)
        apply_theme = self.source.index('apply_theme(self.terminal, theme', anchor)
        self.assertLess(set_theme, apply_theme)

    def test_drain_feeds_vte_through_the_filter(self):
        self.assertIn(
            'self.terminal.feed(self._contrast_filter.process(payload))', self.source
        )
        self.assertNotIn('self.terminal.feed(payload)', self.source)

    def test_filter_is_reset_before_a_new_stream_generation_starts(self):
        reset_call = self.source.index('self._contrast_filter.reset()')
        controller_start = self.source.index(
            'self.controller.start(columns=columns, rows=rows)', reset_call
        )
        self.assertLess(reset_call, controller_start)


if __name__ == '__main__':
    unittest.main()
