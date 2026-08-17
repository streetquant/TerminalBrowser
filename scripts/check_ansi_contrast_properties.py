#!/usr/bin/env python3
"""Deterministic property and reference-model gate for the SGR contrast filter.

The regular unit suite covers named regressions. This executable gate adds three
orthogonal oracles without third-party dependencies:

* arbitrary pane bytes must be total and invariant under arbitrary chunking;
* supported SGR state transitions must agree with a small independent model;
* parser memory, pending escape storage, and the contrast cache stay bounded.

The seed and case counts are printed so a failure is exactly reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import random
import re
from typing import Iterable

from termdeck.ansi_contrast import (
    MAX_PENDING_BYTES,
    SgrContrastFilter,
)
from termdeck.models import TerminalTheme
from termdeck.ui_theme import (
    TERMINAL_MIN_STREAM_CONTRAST,
    contrast_ratio,
    ensure_legible_theme,
    raise_contrast,
    xterm_indexed_color,
)

_SEED = 0x5444315F53475233
_TRUECOLOUR = re.compile(rb"38;2;([0-9]+);([0-9]+);([0-9]+)")


def _normalise_colour(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return "<unknown>"


def _state_signature(filter_: SgrContrastFilter) -> tuple[object, ...]:
    return (
        filter_._pending,
        filter_._opaque_kind,
        filter_._opaque_string_esc,
        _normalise_colour(filter_._fg),
        _normalise_colour(filter_._bg),
        filter_._reverse,
        filter_._state_tainted,
    )


def _random_cuts(rng: random.Random, length: int, maximum: int = 12) -> list[int]:
    cuts = {0, length}
    for _ in range(rng.randrange(maximum + 1)):
        cuts.add(rng.randrange(length + 1))
    return sorted(cuts)


def _chunked(filter_: SgrContrastFilter, payload: bytes, cuts: Iterable[int]) -> bytes:
    positions = list(cuts)
    return b"".join(
        filter_.process(payload[start:end])
        for start, end in zip(positions, positions[1:])
    )


@dataclass
class ReferenceState:
    foreground: str | None
    background: str | None
    reverse: bool = False


@dataclass(frozen=True)
class Operation:
    kind: str
    values: tuple[int, ...] = ()
    syntax: str = "semicolon"

    def render(self) -> str:
        if self.kind == "reset":
            return "0"
        if self.kind == "reverse_on":
            return "7"
        if self.kind == "reverse_off":
            return "27"
        if self.kind == "fg_default":
            return "39"
        if self.kind == "bg_default":
            return "49"
        if self.kind == "style":
            return "4:3"
        target = "38" if self.kind.startswith("fg_") else "48"
        if self.kind.endswith("rgb"):
            red, green, blue = self.values
            if self.syntax == "colon":
                return f"{target}:2::{red}:{green}:{blue}"
            return f"{target};2;{red};{green};{blue}"
        (index,) = self.values
        if self.syntax == "colon":
            return f"{target}:5:{index}"
        return f"{target};5;{index}"


def _indexed_colour(index: int, palette: tuple[str, ...]) -> str | None:
    if not 0 <= index <= 255:
        return None
    if index < 16:
        return palette[index]
    return xterm_indexed_color(index)


def _apply_reference_sequence(
    state: ReferenceState,
    operations: list[Operation],
    *,
    theme_background: str,
    palette: tuple[str, ...],
) -> tuple[ReferenceState, bool]:
    """Apply the production-supported SGR subset and its end-of-sequence lift."""

    touched = False
    for operation in operations:
        kind = operation.kind
        if kind == "reset":
            state = ReferenceState(None, None, False)
            touched = True
        elif kind == "reverse_on":
            state.reverse = True
        elif kind == "reverse_off":
            state.reverse = False
            touched = True
        elif kind == "fg_default":
            state.foreground = None
            touched = True
        elif kind == "bg_default":
            state.background = None
            touched = True
        elif kind == "style":
            continue
        elif kind in {"fg_rgb", "bg_rgb"}:
            if any(channel < 0 or channel > 255 for channel in operation.values):
                continue  # VTE-invalid no-op
            colour = "#{:02x}{:02x}{:02x}".format(*operation.values)
            if kind.startswith("fg_"):
                state.foreground = colour
            else:
                state.background = colour
            touched = True
        elif kind in {"fg_index", "bg_index"}:
            colour = _indexed_colour(operation.values[0], palette)
            if colour is None:
                continue  # VTE-invalid no-op
            if kind.startswith("fg_"):
                state.foreground = colour
            else:
                state.background = colour
            touched = True
        else:  # pragma: no cover - generator contract
            raise AssertionError(f"unsupported reference operation: {operation}")

    lifted = False
    if touched and not state.reverse and state.foreground is not None:
        background = state.background or theme_background
        if contrast_ratio(state.foreground, background) < TERMINAL_MIN_STREAM_CONTRAST:
            replacement = raise_contrast(
                state.foreground,
                background,
                TERMINAL_MIN_STREAM_CONTRAST,
            )
            lifted = replacement != state.foreground
            state.foreground = replacement
    return state, lifted


def _random_operation(rng: random.Random) -> Operation:
    kind = rng.choice(
        (
            "reset",
            "reverse_on",
            "reverse_off",
            "fg_default",
            "bg_default",
            "style",
            "fg_rgb",
            "bg_rgb",
            "fg_index",
            "bg_index",
        )
    )
    syntax = rng.choice(("semicolon", "colon"))
    if kind.endswith("rgb"):
        boundary = (0, 1, 15, 16, 122, 254, 255, 256)
        return Operation(kind, tuple(rng.choice(boundary) for _ in range(3)), syntax)
    if kind.endswith("index"):
        return Operation(kind, (rng.choice((0, 15, 16, 235, 236, 254, 255, 256)),), syntax)
    return Operation(kind, (), syntax)


def _run_reference_model(rng: random.Random, cases: int) -> None:
    theme = ensure_legible_theme(TerminalTheme())
    palette = tuple(theme.palette)
    for case in range(cases):
        filter_ = SgrContrastFilter(TerminalTheme())
        reference = ReferenceState(None, None, False)
        for step in range(rng.randrange(1, 20)):
            operations = [_random_operation(rng) for _ in range(rng.randrange(1, 6))]
            body = ";".join(operation.render() for operation in operations)
            sequence = b"\x1b[" + body.encode("ascii") + b"m"
            observed = filter_.process(sequence)
            reference, lifted = _apply_reference_sequence(
                reference,
                operations,
                theme_background=theme.background_color,
                palette=palette,
            )
            actual = (
                _normalise_colour(filter_._fg),
                _normalise_colour(filter_._bg),
                filter_._reverse,
                filter_._state_tainted,
            )
            expected = (
                reference.foreground,
                reference.background,
                reference.reverse,
                False,
            )
            if actual != expected:
                raise AssertionError(
                    "reference-state mismatch "
                    f"case={case} step={step} body={body!r} "
                    f"expected={expected!r} actual={actual!r} output={observed!r}"
                )
            if lifted:
                if reference.foreground is None:
                    raise AssertionError("lifted reference state lost its foreground")
                red = int(reference.foreground[1:3], 16)
                green = int(reference.foreground[3:5], 16)
                blue = int(reference.foreground[5:7], 16)
                replacement = f"38;2;{red};{green};{blue}".encode("ascii")
                if replacement not in observed:
                    raise AssertionError(
                        "reference required a lift but the expected replacement "
                        f"{replacement!r} was absent from {observed!r}"
                    )
                background = reference.background or theme.background_color
                if (
                    contrast_ratio(reference.foreground, background)
                    < TERMINAL_MIN_STREAM_CONTRAST
                ):
                    raise AssertionError(
                        f"emitted colour {reference.foreground} missed the contrast floor "
                        f"on {background}"
                    )


def _run_arbitrary_bytes(rng: random.Random, cases: int) -> None:
    for case in range(cases):
        length = rng.randrange(0, 320)
        payload = bytes(rng.randrange(256) for _ in range(length))
        if case % 997 == 0:
            payload += b"\x1b[" + bytes((0x30 + rng.randrange(10),)) * (
                4100 + rng.randrange(1000)
            )
            if rng.randrange(2):
                payload += b"m"
        whole_filter = SgrContrastFilter(TerminalTheme())
        whole = whole_filter.process(payload)
        split_filter = SgrContrastFilter(TerminalTheme())
        cuts = _random_cuts(rng, len(payload))
        split = _chunked(split_filter, payload, cuts)
        if whole != split or _state_signature(whole_filter) != _state_signature(split_filter):
            raise AssertionError(
                "arbitrary-byte chunk divergence "
                f"case={case} len={len(payload)} cuts={cuts} payload={payload.hex()}"
            )


def _run_mutation_grid() -> int:
    cases = 0
    prefixes = (b"38;2;", b"38:2::", b"48;2;", b"48:2::")
    channels = (0, 15, 16, 254, 255, 256)
    for prefix in prefixes:
        for red in channels:
            for green in (0, 255, 256):
                for blue in (0, 255, 256):
                    separator = b":" if b":" in prefix else b";"
                    body = prefix + separator.join(
                        str(value).encode("ascii") for value in (red, green, blue)
                    )
                    payload = b"\x1b[" + body + b"mX"
                    filter_ = SgrContrastFilter(TerminalTheme())
                    observed = filter_.process(payload)
                    if max(red, green, blue) > 255:
                        if observed != payload or filter_._state_tainted:
                            raise AssertionError(
                                f"invalid RGB was not a byte-identical no-op: {payload!r} -> {observed!r}"
                            )
                    cases += 1
    for target in (b"38", b"48"):
        for separator in (b";", b":"):
            for index in (0, 15, 16, 235, 236, 254, 255, 256):
                payload = b"\x1b[" + separator.join((target, b"5", str(index).encode())) + b"mX"
                filter_ = SgrContrastFilter(TerminalTheme())
                observed = filter_.process(payload)
                if index == 256 and (observed != payload or filter_._state_tainted):
                    raise AssertionError(
                        f"invalid indexed colour was not a byte-identical no-op: {payload!r}"
                    )
                cases += 1
    return cases


def _run_raise_contrast_grid() -> int:
    """Exercise saturated mid-tones where a luminance shortcut chooses wrongly."""

    levels = (0, 64, 128, 192, 255)
    colours = [
        f"#{red:02x}{green:02x}{blue:02x}"
        for red in levels
        for green in levels
        for blue in levels
    ]
    cases = 0
    for background in colours:
        endpoint = max(
            contrast_ratio("#000000", background),
            contrast_ratio("#ffffff", background),
        )
        for foreground in colours:
            for minimum in (3.0, 4.5, 7.0):
                lifted = raise_contrast(foreground, background, minimum)
                required = min(minimum, endpoint)
                if contrast_ratio(lifted, background) + 1e-12 < required:
                    raise AssertionError(
                        "raise_contrast missed an attainable floor: "
                        f"foreground={foreground} background={background} "
                        f"minimum={minimum} lifted={lifted}"
                    )
                if raise_contrast(lifted, background, minimum) != lifted:
                    raise AssertionError(
                        "raise_contrast was not idempotent: "
                        f"foreground={foreground} background={background} "
                        f"minimum={minimum} lifted={lifted}"
                    )
                cases += 1
    return cases


def _run_bounds() -> None:
    filter_ = SgrContrastFilter(TerminalTheme())
    for index in range(800):
        payload = (
            b"\x1b[0;48;2;%d;%d;%d;38;2;%d;%d;%dmX"
            % (
                (index * 3) % 64,
                (index * 5) % 64,
                (index * 7) % 64,
                index % 256,
                (index // 256) % 256,
                (index // 65536) % 256,
            )
        )
        filter_.process(payload)
        if len(filter_._pending) > MAX_PENDING_BYTES:
            raise AssertionError("pending escape storage exceeded its cap")
        if len(filter_._cache) > 512:
            raise AssertionError("contrast cache exceeded its cap")


def main() -> int:
    arbitrary_cases = int(os.environ.get("TERMDECK_PROPERTY_CASES", "50000"))
    reference_cases = int(os.environ.get("TERMDECK_REFERENCE_CASES", "10000"))
    if arbitrary_cases < 1 or reference_cases < 1:
        raise ValueError("property case counts must be positive")
    rng = random.Random(_SEED)
    _run_reference_model(rng, reference_cases)
    _run_arbitrary_bytes(rng, arbitrary_cases)
    mutation_cases = _run_mutation_grid()
    contrast_grid_cases = _run_raise_contrast_grid()
    _run_bounds()
    print(
        json.dumps(
            {
                "ok": True,
                "seed": hex(_SEED),
                "reference_cases": reference_cases,
                "arbitrary_byte_cases": arbitrary_cases,
                "mutation_cases": mutation_cases,
                "raise_contrast_grid_cases": contrast_grid_cases,
                "minimum_contrast": TERMINAL_MIN_STREAM_CONTRAST,
                "pending_cap": MAX_PENDING_BYTES,
                "cache_cap": 512,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
