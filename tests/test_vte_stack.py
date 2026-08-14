from __future__ import annotations

import os
import subprocess
from unittest import TestCase

from termdeck.vte_stack import (
    _without_alternate_screen,
    merge_vte_attach_environment,
    vte_attach_environment,
)


class VteStackTest(TestCase):
    def test_custom_entry_removes_only_screen_buffer_switches(self) -> None:
        source = """# generated
xterm-256color|xterm,
\tcolors#0x100,
\tclear=\\E[H\\E[2J,
\tcup=\\E[%i%p1%d;%p2%dH,
\trmcup=\\E[?1049l,
\tsmcup=\\E[?1049h,
\tsetaf=\\E[%?%p1%{8}%<%t3%p1%d%e38;5;%p1%d%;m,
"""

        transformed = _without_alternate_screen(source)

        self.assertIn("termdeck-vte|TermDeck VTE client", transformed)
        self.assertIn("colors#0x100", transformed)
        self.assertIn("setaf=", transformed)
        self.assertNotIn("smcup=", transformed)
        self.assertNotIn("rmcup=", transformed)

    def test_attach_environment_compiles_a_no_alt_256_colour_entry(self) -> None:
        environment = vte_attach_environment()
        child = os.environ.copy()
        child.update(environment)

        inspected = subprocess.run(
            ["infocmp", "-1", "-x", environment["TERM"]],
            env=child,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout

        self.assertIn(environment["TERM"], {"termdeck-vte", "teraterm-256color"})
        self.assertIn("colors#0x100", inspected)
        self.assertNotIn("smcup=", inspected)
        self.assertNotIn("rmcup=", inspected)
        self.assertEqual(environment["COLORTERM"], "truecolor")

    def test_attach_environment_overrides_an_inherited_tmux_term(self) -> None:
        merged = merge_vte_attach_environment(
            {"TERM": "tmux-256color", "SENTINEL": "kept"}
        )

        self.assertNotEqual(merged["TERM"], "tmux-256color")
        self.assertEqual(merged["SENTINEL"], "kept")
        self.assertEqual(merged["TERM_PROGRAM"], "TermDeck")
