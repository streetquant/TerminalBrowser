#!/usr/bin/env python3
"""Prove SGR contrast and fail-open semantics at the real VTE pixel boundary.

The byte-level suite is necessary but insufficient: the release gate must show
that VTE renders formerly invisible Codex text above 4.5:1 while invalid colour
syntax, explicit backgrounds, reverse video, and unsupported C1 CSI are not
silently assigned new semantics by TermDeck.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import time


@dataclass(frozen=True)
class RenderCase:
    name: str
    payload: bytes
    expected_background: str
    sample_columns: int = 13
    exact_filter_output: bool = False
    forbidden_after_filter: tuple[bytes, ...] = ()
    required_after_filter: tuple[bytes, ...] = ()


def _drain_gtk(Gtk, *, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def _row_metrics(
    pixbuf,
    *,
    row: int,
    char_width: int,
    char_height: int,
    columns: int,
    expected_background: str,
    contrast_ratio,
) -> tuple[float, int, str]:
    channels = pixbuf.get_n_channels()
    rowstride = pixbuf.get_rowstride()
    pixels = bytes(pixbuf.get_pixels())
    y_start = max(0, row * char_height)
    y_end = min(pixbuf.get_height(), y_start + char_height)
    x_end = min(pixbuf.get_width(), columns * char_width)
    colours: Counter[str] = Counter()
    for y in range(y_start, y_end):
        base = y * rowstride
        for x in range(x_end):
            offset = base + x * channels
            red, green, blue = pixels[offset : offset + 3]
            colours[f"#{red:02x}{green:02x}{blue:02x}"] += 1
    if not colours:
        raise AssertionError(f"VTE produced no pixels on row {row}")

    dominant, _count = colours.most_common(1)[0]
    if contrast_ratio(dominant, expected_background) > 1.05:
        raise AssertionError(
            f"row {row} background changed: expected {expected_background}, "
            f"dominant pixel was {dominant}"
        )

    visible = {
        colour: count
        for colour, count in colours.items()
        if contrast_ratio(colour, expected_background) > 1.05
    }
    if not visible:
        raise AssertionError(f"VTE rendered no visible glyph pixels on row {row}")
    peak = max(
        contrast_ratio(colour, expected_background) for colour in visible
    )
    return peak, sum(visible.values()), dominant


def main() -> int:
    from termdeck.ansi_contrast import SgrContrastFilter
    from termdeck.gtk_app import Gdk, Gtk, Vte, apply_theme
    from termdeck.models import TerminalTheme
    from termdeck.ui_theme import TERMINAL_MIN_STREAM_CONTRAST, contrast_ratio

    ready, _argv = Gtk.init_check([])
    if not ready:
        raise RuntimeError("GTK display is unavailable; run this smoke under xvfb-run")

    theme = TerminalTheme(font="Monospace 24")
    background = theme.background_color
    glyphs = b"MMMMMMMMMMMM"
    invalid_rgb = b"\x1b[38;2;256;0;0m"
    invalid_indexed = b"\x1b[38;5;256m"
    c1 = bytes((0x9B,))
    cases = (
        RenderCase(
            "semicolon_truecolour",
            b"\x1b[38;2;16;16;16m" + glyphs + b"\x1b[0m",
            background,
            forbidden_after_filter=(b"38;2;16;16;16",),
        ),
        RenderCase(
            "colon_truecolour",
            b"\x1b[38:2::16:16:16m" + glyphs + b"\x1b[0m",
            background,
            forbidden_after_filter=(b"38:2::16:16:16",),
        ),
        RenderCase(
            "faint_colon_truecolour",
            b"\x1b[2;38:2::16:16:16m" + glyphs + b"\x1b[0m",
            background,
            forbidden_after_filter=(b"38:2::16:16:16",),
        ),
        RenderCase(
            "colon_indexed_grey",
            b"\x1b[38:5:236m" + glyphs + b"\x1b[0m",
            background,
            forbidden_after_filter=(b"38:5:236",),
        ),
        RenderCase(
            "invalid_rgb_noop",
            invalid_rgb + glyphs + b"\x1b[0m",
            background,
            exact_filter_output=True,
            required_after_filter=(invalid_rgb,),
        ),
        RenderCase(
            "invalid_indexed_noop",
            invalid_indexed + glyphs + b"\x1b[0m",
            background,
            exact_filter_output=True,
            required_after_filter=(invalid_indexed,),
        ),
        RenderCase(
            "reverse_invalid_rgb_recovery",
            b"\x1b[7;38;2;16;16;16m"
            + invalid_rgb
            + b"\x1b[27m"
            + glyphs
            + b"\x1b[0m",
            background,
            required_after_filter=(invalid_rgb,),
        ),
        RenderCase(
            "explicit_light_background",
            b"\x1b[48;2;255;255;255;38;2;16;16;16m"
            + glyphs
            + b"\x1b[0m",
            "#ffffff",
            exact_filter_output=True,
        ),
        RenderCase(
            "reverse_video_preserved",
            b"\x1b[7;48;2;255;255;255;38;2;16;16;16m"
            + glyphs
            + b"\x1b[0m",
            "#101010",
            exact_filter_output=True,
        ),
        RenderCase(
            "c1_csi_not_invented",
            c1 + b"38:2::16:16:16m" + glyphs + c1 + b"0m",
            background,
            exact_filter_output=True,
        ),
    )

    terminal = Vte.Terminal()
    terminal.set_size(40, len(cases) + 1)
    apply_theme(terminal, theme, font_override="Monospace 24")
    window = Gtk.Window()
    window.add(terminal)
    window.resize(1100, 760)
    window.show_all()

    contrast_filter = SgrContrastFilter(theme)
    filtered: list[tuple[RenderCase, bytes]] = []
    try:
        for case in cases:
            rendered = contrast_filter.process(case.payload)
            if case.exact_filter_output and rendered != case.payload:
                raise AssertionError(
                    f"{case.name} changed despite a byte-identical contract: "
                    f"{case.payload!r} -> {rendered!r}"
                )
            for forbidden in case.forbidden_after_filter:
                if forbidden in rendered:
                    raise AssertionError(
                        f"{case.name} bypassed the contrast rewrite: {rendered!r}"
                    )
            for required in case.required_after_filter:
                if required not in rendered:
                    raise AssertionError(
                        f"{case.name} lost fail-open bytes {required!r}: {rendered!r}"
                    )
            filtered.append((case, rendered))
            terminal.feed(rendered + b"\r\n")
        _drain_gtk(Gtk)

        pixbuf = Gdk.pixbuf_get_from_window(
            terminal.get_window(),
            0,
            0,
            terminal.get_allocated_width(),
            terminal.get_allocated_height(),
        )
        if pixbuf is None:
            raise AssertionError("could not capture the real VTE surface")

        char_width = int(terminal.get_char_width())
        char_height = int(terminal.get_char_height())
        results = []
        for row, (case, rendered) in enumerate(filtered):
            peak, visible_pixels, dominant = _row_metrics(
                pixbuf,
                row=row,
                char_width=char_width,
                char_height=char_height,
                columns=case.sample_columns,
                expected_background=case.expected_background,
                contrast_ratio=contrast_ratio,
            )
            if peak < TERMINAL_MIN_STREAM_CONTRAST:
                raise AssertionError(
                    f"{case.name} rendered at only {peak:.3f}:1 on "
                    f"{case.expected_background}; needs "
                    f"{TERMINAL_MIN_STREAM_CONTRAST}:1"
                )
            results.append(
                {
                    "case": case.name,
                    "filtered": rendered.decode("ascii", errors="backslashreplace"),
                    "expected_background": case.expected_background,
                    "dominant_background": dominant,
                    "peak_contrast": round(peak, 3),
                    "visible_pixels": visible_pixels,
                    "byte_identical": rendered == case.payload,
                }
            )

        print(
            json.dumps(
                {
                    "ok": True,
                    "background": background,
                    "minimum_contrast": TERMINAL_MIN_STREAM_CONTRAST,
                    "char_width": char_width,
                    "char_height": char_height,
                    "cases": results,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        window.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
