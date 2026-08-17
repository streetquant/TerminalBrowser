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
import re
import unittest
from pathlib import Path

from termdeck.ansi_contrast import (
    MAX_PENDING_BYTES,
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

    def test_unsupported_bodies_are_rejected_rather_than_guessed(self):
        self.assertIsNone(parse_sgr_groups(b'38;9;1'))
        self.assertIsNone(parse_sgr_groups(b'38;2;1;2'))
        self.assertIsNone(parse_sgr_groups(b'1;?'))


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

    def test_reverse_video_is_never_rewritten(self):
        payload = b'\x1b[7;38;2;16;16;16mreversed\x1b[27m'
        self.assertEqual(self.filter.process(payload), payload)

    def test_dark_ink_on_an_explicit_light_background_is_kept(self):
        '''A program painting its own light background is self-consistent.'''

        payload = b'\x1b[48;2;255;255;255;38;2;16;16;16mreceipt'
        self.assertEqual(self.filter.process(payload), payload)


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
