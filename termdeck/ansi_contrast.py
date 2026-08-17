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

#: A partial escape sequence longer than this is malformed; flush it verbatim
#: rather than withholding pane output while waiting for a terminator.
MAX_PENDING_BYTES = 4096
_CACHE_LIMIT = 512


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
        while index < length:
            byte = data[index]
            if _CSI_FINAL_MIN <= byte <= _CSI_FINAL_MAX:
                return index + 1, ("sgr" if byte == _SGR_FINAL else "other")
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


def parse_sgr_groups(body: bytes) -> list[list[str]] | None:
    """Split an SGR body into parameter groups, or ``None`` if unsupported.

    Extended colour specifications stay together as one group so the caller can
    replace a whole ``38;2;r;g;b`` run atomically.
    """

    if not body:
        return [["0"]]
    tokens = body.split(b";")
    groups: list[list[str]] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token and not token.isdigit():
            return None
        group = [token.decode("ascii") if token else "0"]
        code = int(group[0])
        if code in (38, 48):
            if index + 1 >= total:
                return None
            kind_token = tokens[index + 1]
            if not kind_token.isdigit():
                return None
            kind = int(kind_token)
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
                if raw and not raw.isdigit():
                    return None
                group.append(raw.decode("ascii") if raw else "0")
            index = end
        else:
            index += 1
        groups.append(group)
    return groups


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

    def reset(self) -> None:
        """Drop buffered bytes and forget SGR state (new stream generation)."""

        self._pending = b""
        self._fg: str | None = None
        self._bg: str | None = None
        self._reverse = False

    # -- stream -----------------------------------------------------------
    def process(self, chunk: bytes) -> bytes:
        if not chunk and not self._pending:
            return b""
        data = self._pending + bytes(chunk)
        self._pending = b""
        if _ESC not in data:
            return data
        out = bytearray()
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
                    out += tail
                break
            if kind == "sgr":
                out += self._rewrite_sgr(bytes(data[start:end]))
            else:
                out += data[start:end]
            index = end
        return bytes(out)

    # -- internals --------------------------------------------------------
    def _rewrite_sgr(self, sequence: bytes) -> bytes:
        body = sequence[2:-1]
        if b":" in body:
            # Colon sub-parameter form is rare and rewriting it safely is not
            # worth the corruption risk: pass it through and forget the tracked
            # foreground rather than guess at it.
            self._fg = None
            return sequence
        groups = parse_sgr_groups(body)
        if groups is None:
            self._fg = None
            return sequence
        touched = False
        fg_slot = -1
        for position, group in enumerate(groups):
            code = int(group[0])
            if code == 0:
                self._fg = None
                self._bg = None
                self._reverse = False
                touched = True
                fg_slot = -1
            elif code == 7:
                self._reverse = True
            elif code == 27:
                self._reverse = False
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
                self._fg = self._spec_color(group)
                touched = True
                fg_slot = position
            elif code == 48:
                self._bg = self._spec_color(group)
                touched = True
        if not touched or self._reverse:
            return sequence
        current = self._fg
        if current is None:
            # The theme foreground already clears TERMINAL_MIN_FG_CONTRAST.
            return sequence
        lifted = self._lift(current, self._bg or self._background)
        if lifted is None or lifted == current:
            return sequence
        rgb = _parse_hex_color(lifted)
        if rgb is None:
            return sequence
        self._fg = lifted
        replacement = ["38", "2", str(rgb[0]), str(rgb[1]), str(rgb[2])]
        if fg_slot >= 0:
            groups[fg_slot] = replacement
        else:
            groups.append(replacement)
        rendered = ";".join(";".join(group) for group in groups)
        return b"\x1b[" + rendered.encode("ascii") + b"m"

    def _palette_color(self, index: int) -> str | None:
        if 0 <= index < len(self._palette):
            return self._palette[index]
        return None

    def _spec_color(self, group: list[str]) -> str | None:
        if len(group) >= 3 and group[1] == "5":
            index = int(group[2])
            if index < 16:
                return self._palette_color(index)
            return xterm_indexed_color(index)
        if len(group) >= 5 and group[1] == "2":
            channels = [max(0, min(255, int(value))) for value in group[2:5]]
            return "#{:02x}{:02x}{:02x}".format(*channels)
        return None

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
