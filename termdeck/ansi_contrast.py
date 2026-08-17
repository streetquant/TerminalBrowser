"""Stream-level minimum contrast for terminal output.

VTE's colour palette only covers ANSI slots 0-15. A program that paints text
with an indexed (``38;5;n``) or truecolour (``38;2;r;g;b``) foreground chooses
its own absolute colour, so no palette or theme setting can rescue it: Codex
does exactly that when it mis-detects a dark terminal as light and styles
headings in near-black ink. The only place those colours can still be fixed is
the byte stream the app feeds into VTE, which this module owns.

The filter is a byte-level scanner that rewrites *only* SGR foreground colours
failing ``TERMINAL_MIN_STREAM_CONTRAST`` against the effective background.
Everything else - cursor motion, OSC/DCS strings, backgrounds, bold and italic
attributes - passes through byte-identical, and an escape sequence split across
two reads is buffered instead of mangled.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TerminalTheme
from .ui_theme import (
    TERMINAL_MIN_STREAM_CONTRAST,
    _parse_hex_color,
    contrast_ratio,
    ensure_legible_theme,
    raise_contrast,
    xterm_indexed_color,
)

_ESC = 0x1B
_BEL = 0x07
_CSI = 0x5B
_ST_FINAL = 0x5C
_SGR_FINAL = 0x6D
_CSI_FINAL_MIN = 0x40
_CSI_FINAL_MAX = 0x7E
_STRING_INTRODUCERS = frozenset(b"]P^_X")
_CANCEL_BYTES = frozenset((0x18, 0x1A))  # CAN, SUB

#: A partial escape sequence longer than this is malformed; flush it verbatim
#: rather than withholding pane output while waiting for a terminator.
MAX_PENDING_BYTES = 4096

#: Complete SGR bodies above this size pass through without structural parsing.
#: Scanning remains linear so the final byte can be located, but no split/int
#: allocation scales with an attacker-controlled multi-megabyte parameter list.
MAX_SGR_BYTES = 1024

#: One numeric token can never exceed the already-bounded complete SGR body.
#: Keeping a separate named limit makes the conversion contract explicit while
#: allowing long all-decimal VTE-invalid parameters to remain exact no-ops
#: instead of being confused with malformed syntax.
MAX_SGR_PARAMETER_DIGITS = MAX_SGR_BYTES
_CACHE_LIMIT = 512

#: Tracked SGR state whose effective colour cannot be interpreted safely. This
#: differs from ``None``, which means the terminal's known default colour.
_UNKNOWN_COLOR = object()

#: A syntactically numeric colour outside VTE's accepted domain. VTE ignores
#: this group, so the filter must preserve the previous tracked state too.
_INVALID_COLOR = object()


def _parse_decimal(
    value: bytes | str,
    *,
    maximum: int | None = None,
) -> int | None:
    """Parse one bounded ASCII decimal without invoking unbounded ``int``.

    ``None`` covers empty, non-ASCII, overlong, and above-domain values. Leading
    zeroes remain valid while still counting against the digit limit.
    """

    if isinstance(value, bytes):
        raw = value
    else:
        try:
            raw = value.encode("ascii", "strict")
        except UnicodeEncodeError:
            return None
    if not raw or len(raw) > MAX_SGR_PARAMETER_DIGITS:
        return None
    result = 0
    for byte in raw:
        if byte < 0x30 or byte > 0x39:
            return None
        result = (result * 10) + (byte - 0x30)
        if maximum is not None and result > maximum:
            return None
    return result


def scan_sequence(data: bytes, start: int) -> tuple[int | None, str | None]:
    """Return ``(end, kind)`` for the escape sequence at ``start``.

    ``end`` is exclusive. ``kind`` is ``"sgr"`` for a colour/attribute select
    sequence and ``"other"`` for anything else. ``(None, None)`` means the
    sequence is truncated and the caller must wait for more bytes.
    """

    length = len(data)
    if start + 1 >= length:
        return None, None
    introducer = data[start + 1]
    if introducer == _CSI:
        index = start + 2
        oversized = False
        while index < length:
            if index + 1 - start > MAX_SGR_BYTES:
                oversized = True
            byte = data[index]
            if byte == _ESC:
                # ESC cancels the incomplete CSI and starts a new escape
                # sequence. Leave it unconsumed so the caller can scan the new
                # sequence instead of swallowing its introducer as CSI data.
                return index, "other"
            if byte in _CANCEL_BYTES:
                return index + 1, "other"
            if _CSI_FINAL_MIN <= byte <= _CSI_FINAL_MAX:
                if byte == _SGR_FINAL:
                    return index + 1, ("sgr-oversized" if oversized else "sgr")
                return index + 1, "other"
            index += 1
        return None, None
    if introducer in _STRING_INTRODUCERS:
        index = start + 2
        while index < length:
            byte = data[index]
            if byte == _BEL:
                return index + 1, "other"
            if byte == _ESC:
                if index + 1 >= length:
                    return None, None
                if data[index + 1] == _ST_FINAL:
                    return index + 2, "other"
            index += 1
        return None, None
    if 0x20 <= introducer <= 0x2F:
        index = start + 2
        while index < length:
            if 0x30 <= data[index] <= 0x7E:
                return index + 1, "other"
            index += 1
        return None, None
    return start + 2, "other"


@dataclass(frozen=True)
class _SgrGroup:
    """One top-level SGR parameter, retaining its original sub-separator."""

    values: tuple[str, ...]
    separator: str = ";"

    def render(self) -> str:
        return self.separator.join(self.values)


def _parse_sgr_groups(body: bytes) -> list[_SgrGroup] | None:
    """Parse bounded SGR groups without losing ISO colon subparameters.

    VTE accepts direct colours such as ``38:2::r:g:b``. A semicolon-only
    parser treated every colon body as opaque, which let near-black Codex ink
    bypass the contrast floor. Top-level groups remain semicolon-delimited;
    colon subparameters stay inside one group so unrelated attributes such as
    underline style ``4:3`` can be preserved byte-for-byte during a rewrite.

    This parser is deliberately total: overlong/non-ASCII decimal parameters
    return ``None`` rather than reaching Python's unbounded integer conversion.
    """

    if not body:
        return [_SgrGroup(("0",))]
    if len(body) + 3 > MAX_SGR_BYTES:
        return None
    tokens = body.split(b";")
    groups: list[_SgrGroup] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if b":" in token:
            raw_values = token.split(b":")
            if not raw_values or _parse_decimal(raw_values[0]) is None:
                return None
            values: list[str] = []
            for position, raw in enumerate(raw_values):
                if raw and _parse_decimal(raw) is None:
                    return None
                if position == 0:
                    values.append(raw.decode("ascii") if raw else "0")
                else:
                    # Empty colon subparameters are meaningful. The common
                    # direct-colour form leaves the colour-space id blank.
                    values.append(raw.decode("ascii") if raw else "")
            groups.append(_SgrGroup(tuple(values), ":"))
            index += 1
            continue
        if token and _parse_decimal(token) is None:
            return None
        values = [token.decode("ascii") if token else "0"]
        code = _parse_decimal(values[0])
        if code is None:
            return None
        if code in (38, 48):
            if index + 1 >= total:
                return None
            kind_token = tokens[index + 1]
            kind = _parse_decimal(kind_token)
            if kind == 5:
                extra = 1
            elif kind == 2:
                extra = 3
            else:
                return None
            end = index + 2 + extra
            if end > total:
                return None
            for raw in tokens[index + 1 : end]:
                if raw and _parse_decimal(raw) is None:
                    return None
                values.append(raw.decode("ascii") if raw else "0")
            index = end
        else:
            index += 1
        groups.append(_SgrGroup(tuple(values)))
    return groups


def parse_sgr_groups(body: bytes) -> list[list[str]] | None:
    """Public test/debug view of parsed SGR parameter groups."""

    groups = _parse_sgr_groups(body)
    if groups is None:
        return None
    return [list(group.values) for group in groups]


class SgrContrastFilter:
    """Enforce a foreground contrast floor on a terminal byte stream."""

    def __init__(
        self,
        theme: TerminalTheme | None = None,
        *,
        minimum: float = TERMINAL_MIN_STREAM_CONTRAST,
    ) -> None:
        self._minimum = float(minimum)
        self._cache: dict[tuple[str, str], str] = {}
        self._pending = b""
        self.set_theme(theme if theme is not None else TerminalTheme())
        self.reset()

    # -- configuration ----------------------------------------------------
    def set_theme(self, theme: TerminalTheme) -> None:
        legible = ensure_legible_theme(theme)
        self._background = legible.background_color
        self._foreground = legible.foreground_color
        self._palette = tuple(legible.palette)
        self._cache.clear()

    def _reset_sgr_state(self) -> None:
        self._fg: str | object | None = None
        self._bg: str | object | None = None
        self._reverse = False
        self._state_tainted = False

    def _taint_sgr_state(self) -> None:
        """Mark foreground/background/reverse as unknowable until SGR reset 0."""

        self._fg = _UNKNOWN_COLOR
        self._bg = _UNKNOWN_COLOR
        self._reverse = False
        self._state_tainted = True

    def reset(self) -> None:
        """Drop buffered bytes and forget SGR state (new stream generation)."""

        self._pending = b""
        self._opaque_kind: str | None = None
        self._opaque_string_esc = False
        self._reset_sgr_state()

    # -- stream -----------------------------------------------------------
    def process(self, chunk: bytes) -> bytes:
        if not chunk and not self._pending:
            return b""

        incoming = bytes(chunk)
        out = bytearray()
        if self._opaque_kind is not None:
            consumed = self._consume_opaque_prefix(incoming, out)
            if consumed >= len(incoming):
                return bytes(out)
            incoming = incoming[consumed:]

        data = self._pending + incoming
        self._pending = b""
        if _ESC not in data:
            out += data
            return bytes(out)
        index = 0
        length = len(data)
        while index < length:
            start = data.find(_ESC, index)
            if start < 0:
                out += data[index:]
                break
            out += data[index:start]
            end, kind = scan_sequence(data, start)
            if end is None:
                tail = data[start:]
                if len(tail) <= MAX_PENDING_BYTES:
                    self._pending = bytes(tail)
                else:
                    self._begin_opaque_passthrough(tail)
                    out += tail
                break
            if kind == "sgr":
                out += self._rewrite_sgr(bytes(data[start:end]))
            elif kind == "sgr-oversized":
                self._taint_sgr_state()
                out += data[start:end]
            else:
                out += data[start:end]
            index = end
        return bytes(out)

    # -- internals --------------------------------------------------------
    @staticmethod
    def _incomplete_sequence_kind(tail: bytes) -> str | None:
        if len(tail) < 2 or tail[0] != _ESC:
            return None
        introducer = tail[1]
        if introducer == _CSI:
            return "csi"
        if introducer in _STRING_INTRODUCERS:
            return "string"
        if 0x20 <= introducer <= 0x2F:
            return "escape"
        return None

    def _begin_opaque_passthrough(self, tail: bytes) -> None:
        """Bound memory while retaining the terminal parser's sequence boundary.

        Once an incomplete escape grows beyond ``MAX_PENDING_BYTES`` it is
        emitted verbatim, but later chunks can still belong to that same CSI or
        control string. Remember only the grammar class (and a split ST marker)
        so embedded SGR-looking bytes are never rewritten as ordinary output.
        """

        self._opaque_kind = self._incomplete_sequence_kind(tail)
        self._opaque_string_esc = bool(
            self._opaque_kind == "string" and tail.endswith(bytes((_ESC,)))
        )

    def _clear_opaque_passthrough(self) -> None:
        self._opaque_kind = None
        self._opaque_string_esc = False

    def _consume_opaque_prefix(self, data: bytes, out: bytearray) -> int:
        """Emit the continuation of an already-flushed escape byte-identically."""

        kind = self._opaque_kind
        if kind is None:
            return 0
        if not data:
            return 0

        if kind == "string":
            index = 0
            if self._opaque_string_esc:
                self._opaque_string_esc = False
                if data[0] == _ST_FINAL:
                    out += data[:1]
                    self._clear_opaque_passthrough()
                    return 1
            while index < len(data):
                byte = data[index]
                if byte == _BEL or byte in _CANCEL_BYTES:
                    out += data[: index + 1]
                    self._clear_opaque_passthrough()
                    return index + 1
                if byte == _ESC:
                    if index + 1 >= len(data):
                        out += data
                        self._opaque_string_esc = True
                        return len(data)
                    if data[index + 1] == _ST_FINAL:
                        out += data[: index + 2]
                        self._clear_opaque_passthrough()
                        return index + 2
                index += 1
            out += data
            return len(data)

        if kind == "csi":
            for index, byte in enumerate(data):
                if byte == _ESC:
                    # The nested ESC cancels the old CSI. Do not consume it;
                    # normal scanning below must see the new sequence.
                    out += data[:index]
                    self._clear_opaque_passthrough()
                    return index
                if byte in _CANCEL_BYTES:
                    out += data[: index + 1]
                    self._clear_opaque_passthrough()
                    return index + 1
                if _CSI_FINAL_MIN <= byte <= _CSI_FINAL_MAX:
                    out += data[: index + 1]
                    self._clear_opaque_passthrough()
                    if byte == _SGR_FINAL:
                        # The complete SGR was too large to parse, so its
                        # foreground/background/reverse effect is unknowable.
                        self._taint_sgr_state()
                    return index + 1
            out += data
            return len(data)

        # ESC with intermediate bytes: preserve it until its final byte, CAN,
        # SUB, or a nested ESC starts a replacement sequence.
        for index, byte in enumerate(data):
            if byte == _ESC:
                out += data[:index]
                self._clear_opaque_passthrough()
                return index
            if byte in _CANCEL_BYTES or 0x30 <= byte <= 0x7E:
                out += data[: index + 1]
                self._clear_opaque_passthrough()
                return index + 1
        out += data
        return len(data)

    def _rewrite_sgr(self, sequence: bytes) -> bytes:
        if len(sequence) > MAX_SGR_BYTES:
            self._taint_sgr_state()
            return sequence
        body = sequence[2:-1]
        groups = _parse_sgr_groups(body)
        if groups is None:
            # Structurally unsupported syntax may affect foreground,
            # background, or reverse video. Do not guess until a full reset
            # re-establishes all three pieces of state.
            self._taint_sgr_state()
            return sequence
        touched = False
        fg_slot = -1
        for position, group in enumerate(groups):
            code = _parse_decimal(group.values[0])
            if code is None:
                self._taint_sgr_state()
                return sequence

            if self._state_tainted:
                if code != 0:
                    continue
                self._reset_sgr_state()
                touched = True
                fg_slot = -1
                continue

            if code == 0:
                self._reset_sgr_state()
                touched = True
                fg_slot = -1
            elif code == 7:
                self._reverse = True
            elif code == 27:
                self._reverse = False
                # A low foreground selected while reverse video was active
                # becomes normal ink again at this boundary. Re-evaluate it
                # here even when SGR 27 carries no new colour of its own.
                touched = True
            elif code == 39:
                self._fg = None
                touched = True
                fg_slot = position
            elif code == 49:
                self._bg = None
                touched = True
            elif 30 <= code <= 37:
                self._fg = self._palette_color(code - 30)
                touched = True
                fg_slot = position
            elif 90 <= code <= 97:
                self._fg = self._palette_color(code - 82)
                touched = True
                fg_slot = position
            elif 40 <= code <= 47:
                self._bg = self._palette_color(code - 40)
                touched = True
            elif 100 <= code <= 107:
                self._bg = self._palette_color(code - 92)
                touched = True
            elif code == 38:
                color = self._spec_color(group)
                if color is _INVALID_COLOR:
                    # VTE ignores numeric colours outside its accepted domain;
                    # preserve both bytes and the previous effective ink.
                    continue
                self._fg = color
                touched = True
                fg_slot = position if isinstance(color, str) else -1
            elif code == 48:
                color = self._spec_color(group)
                if color is _INVALID_COLOR:
                    continue
                self._bg = color
                touched = True
        if self._state_tainted or not touched or self._reverse:
            return sequence
        current = self._fg
        if current is None:
            # The theme foreground already clears TERMINAL_MIN_FG_CONTRAST.
            return sequence
        if not isinstance(current, str):
            # Unknown colour spaces and unsupported forms pass through rather
            # than being guessed into a potentially incorrect foreground.
            return sequence
        if self._bg is None:
            background = self._background
        elif isinstance(self._bg, str):
            background = self._bg
        else:
            return sequence
        lifted = self._lift(current, background)
        if lifted is None or lifted == current:
            return sequence
        rgb = _parse_hex_color(lifted)
        if rgb is None:
            return sequence
        self._fg = lifted
        replacement = _SgrGroup(
            ("38", "2", str(rgb[0]), str(rgb[1]), str(rgb[2]))
        )
        if fg_slot >= 0:
            groups[fg_slot] = replacement
        else:
            groups.append(replacement)
        rendered = ";".join(group.render() for group in groups)
        return b"\x1b[" + rendered.encode("ascii") + b"m"

    def _palette_color(self, index: int) -> str | None:
        if 0 <= index < len(self._palette):
            return self._palette[index]
        return None

    def _spec_color(self, group: _SgrGroup) -> str | object:
        values = group.values
        if len(values) < 2:
            return _UNKNOWN_COLOR
        kind = _parse_decimal(values[1])
        if kind == 5:
            if len(values) != 3 or not values[2]:
                return _UNKNOWN_COLOR
            index = _parse_decimal(values[2])
            if index is None:
                return _UNKNOWN_COLOR
            if index > 255:
                return _INVALID_COLOR
            if index < 16:
                return self._palette_color(index) or _UNKNOWN_COLOR
            return xterm_indexed_color(index) or _UNKNOWN_COLOR
        if kind == 2:
            if group.separator == ":":
                tail = values[2:]
                if len(tail) == 3:
                    raw_channels = tail
                elif len(tail) == 4 and tail[0] in ("", "0"):
                    raw_channels = tail[1:]
                else:
                    # Non-default colour spaces require conversion semantics
                    # TermDeck does not own, so preserve the original SGR and
                    # mark only this colour field unknown.
                    return _UNKNOWN_COLOR
            else:
                raw_channels = values[2:5]
            if len(raw_channels) != 3 or any(not value for value in raw_channels):
                return _UNKNOWN_COLOR
            channels: list[int] = []
            for value in raw_channels:
                channel = _parse_decimal(value)
                if channel is None:
                    return _UNKNOWN_COLOR
                if channel > 255:
                    return _INVALID_COLOR
                channels.append(channel)
            return "#{:02x}{:02x}{:02x}".format(*channels)
        return _UNKNOWN_COLOR

    def _lift(self, color: str, background: str) -> str | None:
        key = (color, background)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            ratio = contrast_ratio(color, background)
        except Exception:
            return None
        result = color if ratio >= self._minimum else raise_contrast(
            color, background, self._minimum
        )
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = result
        return result
