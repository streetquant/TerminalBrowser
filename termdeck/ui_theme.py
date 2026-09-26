"""Modern aesthetic tokens, CSS chrome, and built-in VTE palette packs.

Pure helpers stay display-free so unit tests can cover settings and style
payloads without opening a GTK window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from .chrome_css import build_modern_chrome_css
from .models import DEFAULT_PALETTE, TerminalTheme

CHROME_THEME_NAMES = ("dark", "noir", "light", "system")
DENSITY_NAMES = ("comfortable", "compact")
CURSOR_SHAPE_NAMES = ("block", "underline", "ibeam")
TERMINAL_PALETTE_NAMES = (
    "t1000",
    "terminator",
    "catppuccin",
    "tokyo-night",
    "nord",
    "one-dark",
    "solarized-dark",
)

# Full-screen terminal applications often contain bounded cards, composers, or
# reading surfaces even when the terminal itself uses every column. Allowing the
# font to fall to 6pt turns a maximized window into a 200+ column grid where
# those surfaces look stranded and the text is no longer comfortably readable.
MIN_TERMINAL_FONT_SIZE = 10.0
MAX_TERMINAL_FONT_SIZE = 72.0

# Kind cues for notebook tabs / launcher rows (emoji keeps GTK3 dependency-free).
KIND_GLYPHS = {
    "shell": "$",
    "node": "⬢",
    "python": "py",
    "docker": "dk",
    "ssh": "ssh",
    "ai": "✦",
    "test": "✓",
    "log": "log",
    "repl": "λ",
    "task": "▸",
    "default": "■",
}


@dataclass(frozen=True)
class ChromeTokens:
    """Semantic colors for GTK chrome (not VTE)."""

    name: str
    bg: str
    bg_raised: str
    bg_sunken: str
    fg: str
    fg_muted: str
    border: str
    accent: str
    accent_fg: str
    danger: str
    warning: str
    success: str
    card_radius: int = 3
    mono: str = "Monospace"


CHROME_TOKENS: Mapping[str, ChromeTokens] = {
    "dark": ChromeTokens(
        name="dark",
        # Neutral graphite keeps the working surface quiet. Amber marks
        # focus and selection; terminal palette choices remain independent.
        bg="#151514",
        bg_raised="#1e1e1c",
        bg_sunken="#10100f",
        fg="#e5e1d8",
        fg_muted="#aaa69c",
        border="#373731",
        accent="#d7b477",
        accent_fg="#151514",
        danger="#ef9190",
        warning="#d7b477",
        success="#a7bba3",
    ),
    "noir": ChromeTokens(
        name="noir",
        # A deeper neutral variant retains the existing noir preference.
        bg="#10100f",
        bg_raised="#181817",
        bg_sunken="#0b0b0a",
        fg="#e5e1d8",
        fg_muted="#aaa69c",
        border="#30302b",
        accent="#d7b477",
        accent_fg="#151514",
        danger="#ef9190",
        warning="#d7b477",
        success="#a7bba3",
    ),
    "light": ChromeTokens(
        name="light",
        # Warm paper with a dark amber accent; text contrast is tested below.
        bg="#f6f5f1",
        bg_raised="#ffffff",
        bg_sunken="#edece6",
        fg="#282822",
        fg_muted="#68665e",
        border="#d8d5cb",
        accent="#805b1a",
        accent_fg="#ffffff",
        danger="#a33b34",
        warning="#805b1a",
        success="#3e674b",
    ),
    "system": ChromeTokens(
        name="system",
        # System preference continues to select the matching light/dark pack.
        bg="#f6f5f1",
        bg_raised="#ffffff",
        bg_sunken="#edece6",
        fg="#282822",
        fg_muted="#68665e",
        border="#d8d5cb",
        accent="#805b1a",
        accent_fg="#ffffff",
        danger="#a33b34",
        warning="#805b1a",
        success="#3e674b",
    ),
}


# Named 16-color VTE packs + fg/bg. Values are modern community palettes.
TERMINAL_PALETTE_PACKS: Mapping[str, dict[str, object]] = {
    "t1000": {
        # Terminal colors are independent of the surrounding application chrome.
        "label": "t1000 Ink (signature)",
        "foreground": "#dfe6f2",
        "background": "#0a0d13",
        "cursor": "#82aaff",
        "palette": DEFAULT_PALETTE,
    },
    "terminator": {
        "label": "Terminator / system default",
        "foreground": "#d3d7cf",
        "background": "#000000",
        "cursor": "#d3d7cf",
        "palette": DEFAULT_PALETTE,
    },
    "catppuccin": {
        "label": "Catppuccin Mocha",
        "foreground": "#cdd6f4",
        "background": "#1e1e2e",
        "cursor": "#f5e0dc",
        "palette": (
            "#45475a",
            "#f38ba8",
            "#a6e3a1",
            "#f9e2af",
            "#89b4fa",
            "#f5c2e7",
            "#94e2d5",
            "#bac2de",
            "#585b70",
            "#f38ba8",
            "#a6e3a1",
            "#f9e2af",
            "#89b4fa",
            "#f5c2e7",
            "#94e2d5",
            "#a6adc8",
        ),
    },
    "tokyo-night": {
        "label": "Tokyo Night",
        "foreground": "#c0caf5",
        "background": "#1a1b26",
        "cursor": "#c0caf5",
        "palette": (
            "#15161e",
            "#f7768e",
            "#9ece6a",
            "#e0af68",
            "#7aa2f7",
            "#bb9af7",
            "#7dcfff",
            "#a9b1d6",
            "#414868",
            "#f7768e",
            "#9ece6a",
            "#e0af68",
            "#7aa2f7",
            "#bb9af7",
            "#7dcfff",
            "#c0caf5",
        ),
    },
    "nord": {
        "label": "Nord",
        "foreground": "#d8dee9",
        "background": "#2e3440",
        "cursor": "#d8dee9",
        "palette": (
            "#3b4252",
            "#bf616a",
            "#a3be8c",
            "#ebcb8b",
            "#81a1c1",
            "#b48ead",
            "#88c0d0",
            "#e5e9f0",
            "#4c566a",
            "#bf616a",
            "#a3be8c",
            "#ebcb8b",
            "#81a1c1",
            "#b48ead",
            "#8fbcbb",
            "#eceff4",
        ),
    },
    "one-dark": {
        "label": "One Dark",
        "foreground": "#abb2bf",
        "background": "#282c34",
        "cursor": "#528bff",
        "palette": (
            "#5c6370",
            "#e06c75",
            "#98c379",
            "#e5c07b",
            "#61afef",
            "#c678dd",
            "#56b6c2",
            "#abb2bf",
            "#5c6370",
            "#e06c75",
            "#98c379",
            "#e5c07b",
            "#61afef",
            "#c678dd",
            "#56b6c2",
            "#ffffff",
        ),
    },
    "solarized-dark": {
        "label": "Solarized Dark",
        "foreground": "#839496",
        "background": "#002b36",
        "cursor": "#93a1a1",
        "palette": (
            "#073642",
            "#dc322f",
            "#859900",
            "#b58900",
            "#268bd2",
            "#d33682",
            "#2aa198",
            "#eee8d5",
            "#586e75",
            "#cb4b16",
            "#586e75",
            "#657b83",
            "#839496",
            "#6c71c4",
            "#93a1a1",
            "#fdf6e3",
        ),
    },
}


def chrome_theme_names() -> tuple[str, ...]:
    return CHROME_THEME_NAMES


def density_names() -> tuple[str, ...]:
    return DENSITY_NAMES


def terminal_palette_names() -> tuple[str, ...]:
    return TERMINAL_PALETTE_NAMES


def normalise_chrome_theme(name: str | None) -> str:
    cleaned = str(name or "dark").strip().lower()
    if cleaned in {"default", "modern"}:
        return "dark"
    if cleaned in {"day", "paper"}:
        return "light"
    if cleaned not in CHROME_THEME_NAMES:
        return "dark"
    return cleaned


def normalise_density(name: str | None) -> str:
    cleaned = str(name or "comfortable").strip().lower()
    if cleaned in {"cozy", "roomy"}:
        return "comfortable"
    if cleaned in {"dense", "tight"}:
        return "compact"
    if cleaned not in DENSITY_NAMES:
        return "comfortable"
    return cleaned


def normalise_terminal_palette(name: str | None) -> str:
    cleaned = str(name or "t1000").strip().lower().replace("_", "-")
    aliases = {
        "default": "t1000",
        "ink": "t1000",
        "signature": "t1000",
        "system": "terminator",
        "tokyo": "tokyo-night",
        "tokyonight": "tokyo-night",
        "one dark": "one-dark",
        "onedark": "one-dark",
        "solarized": "solarized-dark",
        "catppuccin-mocha": "catppuccin",
        "mocha": "catppuccin",
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in TERMINAL_PALETTE_NAMES:
        return "t1000"
    return cleaned


def cursor_shape_names() -> tuple[str, ...]:
    return CURSOR_SHAPE_NAMES


def normalise_cursor_shape(name: str | None) -> str:
    cleaned = str(name or "block").strip().lower().replace("_", "-").replace(" ", "")
    aliases = {
        "i-beam": "ibeam",
        "ibeam": "ibeam",
        "bar": "ibeam",
        "beam": "ibeam",
        "caret": "ibeam",
        "under": "underline",
        "underscore": "underline",
        "line": "underline",
        "box": "block",
        "blockcursor": "block",
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in CURSOR_SHAPE_NAMES:
        return "block"
    return cleaned


def normalise_font_description(value: str | None, *, default: str = "Monospace 12") -> str:
    """Accept Pango-style font strings such as ``Monospace 13`` or ``JetBrains Mono 12``."""

    text = str(value or "").strip()
    if not text:
        return default
    # Collapse whitespace; keep family + size token(s).
    parts = text.split()
    if len(parts) == 1 and parts[0].isdigit():
        return f"Monospace {parts[0]}"
    return " ".join(parts)


def parse_font_description(font: str | None, *, default: str = "Monospace 12") -> tuple[str, float]:
    """Split a Pango font string into ``(family_and_style, size_pt)``."""

    text = normalise_font_description(font, default=default)
    parts = text.split()
    size = 12.0
    family_parts = parts
    if parts:
        # Prefer a trailing numeric size token (optionally with decimals).
        tail = parts[-1]
        try:
            size = float(tail)
            family_parts = parts[:-1] or ["Monospace"]
        except ValueError:
            family_parts = parts
            size = 12.0
    family = " ".join(family_parts) if family_parts else "Monospace"
    return family, size


def scale_font_description(
    font: str | None,
    delta_pt: float,
    *,
    min_size: float = MIN_TERMINAL_FONT_SIZE,
    max_size: float = MAX_TERMINAL_FONT_SIZE,
    default: str = "Monospace 12",
) -> str:
    """Return a font description with size adjusted by ``delta_pt`` points."""

    family, size = parse_font_description(font, default=default)
    new_size = max(min_size, min(max_size, size + float(delta_pt)))
    # Prefer integer sizes when whole points; keep one decimal otherwise.
    if abs(new_size - round(new_size)) < 1e-6:
        size_token = str(int(round(new_size)))
    else:
        size_token = f"{new_size:.1f}".rstrip("0").rstrip(".")
    return f"{family} {size_token}"


def apply_terminal_overrides(
    theme: TerminalTheme,
    *,
    font: str | None = None,
    use_system_font: bool | None = None,
    cursor_shape: str | None = None,
    cursor_color: str | None = None,
    cursor_blink: bool | None = None,
    scrollback_lines: int | None = None,
) -> TerminalTheme:
    """Overlay minimal terminal chrome options onto a TerminalTheme.

    ``None`` fields keep the current theme value (Terminator import / palette pack).
    """

    updates: dict[str, object] = {}
    if font is not None:
        updates["font"] = normalise_font_description(font, default=theme.font)
    if use_system_font is not None:
        updates["use_system_font"] = bool(use_system_font)
    if cursor_shape is not None:
        updates["cursor_shape"] = normalise_cursor_shape(cursor_shape)
    if cursor_color is not None:
        colour = str(cursor_color).strip()
        updates["cursor_color"] = colour or None
    if cursor_blink is not None:
        updates["cursor_blink"] = bool(cursor_blink)
    if scrollback_lines is not None:
        updates["scrollback_lines"] = max(100, int(scrollback_lines))
    if not updates:
        return theme
    return replace(theme, **updates)  # type: ignore[arg-type]


def resolve_chrome_tokens(name: str | None) -> ChromeTokens:
    return CHROME_TOKENS[normalise_chrome_theme(name)]


# WCAG 2.1 AA thresholds: 4.5:1 for normal text, 3:1 for non-text UI marks.
MIN_TEXT_CONTRAST = 4.5
MIN_UI_CONTRAST = 3.0


def relative_luminance(color: str) -> float:
    """WCAG 2.1 relative luminance of a ``#rrggbb`` hex color."""

    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"expected #rrggbb color, got {color!r}")
    channels = [int(text[index : index + 2], 16) / 255.0 for index in (0, 2, 4)]

    def linearise(channel: float) -> float:
        if channel <= 0.03928:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linearise(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colors (1.0 .. 21.0)."""

    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# Minimum-contrast floors for terminal rendering (VTE). This is the same
# legibility feature the best terminal emulators ship (WezTerm's
# ``minimum_contrast``, iTerm2's "Minimum Contrast"): colors are gently
# blended toward legibility while preserving hue, so a TUI that prints ANSI
# black on a dark background can never render invisible text — including
# themes imported from Terminator configs.
TERMINAL_MIN_PALETTE_CONTRAST = 3.0
TERMINAL_MIN_FG_CONTRAST = 4.5
TERMINAL_MIN_CURSOR_CONTRAST = 3.0
# Colours a program picks itself (indexed or truecolour SGR) bypass the
# palette entirely, so the stream filter holds them to the body-text floor
# rather than the large-text floor used for palette slots.
TERMINAL_MIN_STREAM_CONTRAST = 4.5


def _parse_hex_color(color: str | None) -> tuple[int, int, int] | None:
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None


def _blend_hex(color: str, target: str, amount: float) -> str:
    source = _parse_hex_color(color)
    destination = _parse_hex_color(target)
    if source is None or destination is None:
        return color
    mixed = tuple(
        int(round(start + (end - start) * amount))
        for start, end in zip(source, destination)
    )
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def raise_contrast(color: str, background: str, minimum: float) -> str:
    """Blend ``color`` the shortest distance to a contrast-safe endpoint.

    Already-legible colors are returned untouched and unparseable values fail
    open.  Both black and white are considered because a coarse
    ``background luminance < 0.5`` rule is wrong for saturated mid-tone
    surfaces: for example white reaches only 4.0:1 on ``#107aff`` while black
    reaches 5.25:1.  Among reachable endpoints, choose the smallest RGB blend
    amount so the original hue is preserved as far as the requested floor
    allows.
    """

    if _parse_hex_color(color) is None or _parse_hex_color(background) is None:
        return color
    minimum = max(1.0, float(minimum))
    if contrast_ratio(color, background) >= minimum:
        return color

    candidates: list[tuple[float, str, float]] = []
    for target in ("#000000", "#ffffff"):
        endpoint_ratio = contrast_ratio(target, background)
        if endpoint_ratio < minimum:
            continue
        low, high = 0.0, 1.0
        for _ in range(24):
            middle = (low + high) / 2
            candidate = _blend_hex(color, target, middle)
            if contrast_ratio(candidate, background) >= minimum:
                high = middle
            else:
                low = middle
        lifted = _blend_hex(color, target, high)
        # Eight-bit rounding can leave the binary-search boundary one channel
        # step short. Advance monotonically toward the proven endpoint until
        # the rendered colour reaches the floor.
        if contrast_ratio(lifted, background) < minimum:
            for step in range(1, 257):
                amount = min(1.0, high + step / 255.0)
                lifted = _blend_hex(color, target, amount)
                if contrast_ratio(lifted, background) >= minimum:
                    high = amount
                    break
        ratio = contrast_ratio(lifted, background)
        if ratio >= minimum:
            candidates.append((high, lifted, ratio))

    if candidates:
        # Blend distance is primary; excess contrast is only a stable tie-break.
        _amount, lifted, _ratio = min(candidates, key=lambda item: (item[0], item[2]))
        return lifted

    # A caller may ask for an impossible ratio (>21). Return the endpoint with
    # the strongest attainable contrast rather than an endpoint chosen by an
    # unrelated luminance heuristic.
    return max(
        ("#000000", "#ffffff"),
        key=lambda target: contrast_ratio(target, background),
    )


_XTERM_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def xterm_indexed_color(index: int) -> str | None:
    """Standard xterm-256 color for indices 16-255 (None outside range)."""

    if 16 <= index <= 231:
        value = index - 16
        red = _XTERM_CUBE_LEVELS[value // 36]
        green = _XTERM_CUBE_LEVELS[(value // 6) % 6]
        blue = _XTERM_CUBE_LEVELS[value % 6]
        return "#{:02x}{:02x}{:02x}".format(red, green, blue)
    if 232 <= index <= 255:
        level = 8 + (index - 232) * 10
        return "#{:02x}{:02x}{:02x}".format(level, level, level)
    return None


def greyscale_contrast_overrides(theme: TerminalTheme) -> tuple[tuple[int, str], ...]:
    """xterm-256 grey entries that need lifting against the theme background.

    TUIs (codex, claude, htop, ...) draw dim text with indexed greys from the
    256-color table, which VTE resolves internally - the 16-slot palette
    cannot fix those. Only true greys (r == g == b) are lifted so colored
    cube entries used for accents and panel backgrounds stay untouched.
    """

    background = theme.background_color
    if _parse_hex_color(background) is None:
        return ()
    overrides: list[tuple[int, str]] = []
    for index in range(16, 256):
        color = xterm_indexed_color(index)
        if color is None:
            continue
        red, green, blue = _parse_hex_color(color)
        if not (red == green == blue):
            continue
        lifted = raise_contrast(color, background, TERMINAL_MIN_PALETTE_CONTRAST)
        if lifted != color:
            overrides.append((index, lifted))
    return tuple(overrides)


def osc4_sequence(overrides: tuple[tuple[int, str], ...]) -> bytes:
    """OSC 4 escape stream redefining indexed colors inside VTE."""

    parts: list[str] = []
    for index, color in overrides:
        parsed = _parse_hex_color(color)
        if parsed is None:
            continue
        red, green, blue = parsed
        parts.append(
            "\x1b]4;{};rgb:{:02x}/{:02x}/{:02x}\x07".format(index, red, green, blue)
        )
    return "".join(parts).encode("ascii")


def ensure_legible_theme(theme: TerminalTheme) -> TerminalTheme:
    """Enforce terminal minimum-contrast floors on a TerminalTheme.

    Foreground text must clear ``TERMINAL_MIN_FG_CONTRAST``; all sixteen
    palette entries and the cursor clear their own floors against the theme
    background. Returns the theme unchanged when everything already passes.
    """

    background = theme.background_color
    if _parse_hex_color(background) is None:
        return theme
    updates: dict[str, object] = {}
    foreground = raise_contrast(
        theme.foreground_color, background, TERMINAL_MIN_FG_CONTRAST
    )
    if foreground != theme.foreground_color:
        updates["foreground_color"] = foreground
    palette = tuple(
        raise_contrast(entry, background, TERMINAL_MIN_PALETTE_CONTRAST)
        for entry in theme.palette
    )
    if relative_luminance(background) < 0.5:
        # On dark surfaces ANSI black and bright black render as the theme
        # foreground: "black" text shows white instead of a dark slate.
        bright = list(palette)
        for slot in (0, 8):
            if slot < len(bright):
                bright[slot] = foreground
        palette = tuple(bright)
    if palette != tuple(theme.palette):
        updates["palette"] = palette
    if theme.cursor_color:
        cursor = raise_contrast(
            theme.cursor_color, background, TERMINAL_MIN_CURSOR_CONTRAST
        )
        if cursor != theme.cursor_color:
            updates["cursor_color"] = cursor
    if not updates:
        return theme
    return replace(theme, **updates)  # type: ignore[arg-type]


def contrast_violations(tokens: ChromeTokens) -> list[str]:
    """Human-readable WCAG AA failures for a chrome token pack.

    Text-bearing pairs must clear ``MIN_TEXT_CONTRAST`` (4.5:1); semantic
    non-text hues must clear ``MIN_UI_CONTRAST`` (3:1) against both working
    surfaces. An empty list means the pack is AA-clean.
    """

    text_pairs = (
        ("fg", tokens.fg, "bg", tokens.bg),
        ("fg", tokens.fg, "bg_raised", tokens.bg_raised),
        ("fg", tokens.fg, "bg_sunken", tokens.bg_sunken),
        ("fg_muted", tokens.fg_muted, "bg", tokens.bg),
        ("fg_muted", tokens.fg_muted, "bg_raised", tokens.bg_raised),
        ("accent_fg", tokens.accent_fg, "accent", tokens.accent),
    )
    ui_pairs = (
        ("accent", tokens.accent, "bg", tokens.bg),
        ("accent", tokens.accent, "bg_raised", tokens.bg_raised),
        ("danger", tokens.danger, "bg", tokens.bg),
        ("warning", tokens.warning, "bg", tokens.bg),
        ("success", tokens.success, "bg", tokens.bg),
    )
    failures: list[str] = []
    for fg_name, fg_value, bg_name, bg_value in text_pairs:
        ratio = contrast_ratio(fg_value, bg_value)
        if ratio < MIN_TEXT_CONTRAST:
            failures.append(
                f"{tokens.name}: {fg_name} on {bg_name} is {ratio:.2f}:1 "
                f"(needs {MIN_TEXT_CONTRAST}:1)"
            )
    for hue_name, hue_value, bg_name, bg_value in ui_pairs:
        ratio = contrast_ratio(hue_value, bg_value)
        if ratio < MIN_UI_CONTRAST:
            failures.append(
                f"{tokens.name}: {hue_name} on {bg_name} is {ratio:.2f}:1 "
                f"(needs {MIN_UI_CONTRAST}:1)"
            )
    return failures


def detect_system_prefers_dark() -> bool:
    """Best-effort dark preference for the system chrome theme.

    Falls back to dark when portals/settings are unavailable so headless and
    minimal desktops still get a coherent modern shell.
    """

    try:
        import gi  # type: ignore

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        settings = Gtk.Settings.get_default()
        if settings is None:
            return True
        # Prefer explicit dark preference when present (GTK 3.30+ / portals).
        if settings.get_property("gtk-application-prefer-dark-theme"):
            return True
        theme_name = str(settings.get_property("gtk-theme-name") or "").lower()
        if "dark" in theme_name or theme_name.endswith("-dark"):
            return True
        # Explicit light desktop themes.
        if theme_name in {"adwaita", "highcontrast", "yaru", "breeze", "arc"}:
            return False
        if theme_name in {"highcontrastinverse", "adwaita-dark", "yaru-dark", "breeze-dark"}:
            return True
        # Common light naming without "dark" suffix.
        if any(token in theme_name for token in ("light", "day", "paper")):
            return False
    except Exception:
        return True
    # Unknown desktops: prefer dark chrome (matches TermDeck default).
    return True


def effective_chrome_theme(name: str | None) -> str:
    """Resolve the concrete token pack for a chrome theme setting.

    ``system`` follows the desktop: dark pack when dark is preferred, otherwise
    the light ``system`` token pack (which mirrors the explicit ``light``
    theme). Explicit dark/noir/light are unchanged.
    """

    chosen = normalise_chrome_theme(name)
    if chosen != "system":
        return chosen
    return "dark" if detect_system_prefers_dark() else "system"


def spacing_for_density(density: str | None) -> dict[str, int]:
    if normalise_density(density) == "compact":
        return {
            "window_margin": 4,
            "card_margin": 3,
            "card_padding": 6,
            "row_spacing": 4,
            "header_spacing": 4,
            "toast_seconds": 3,
        }
    return {
        "window_margin": 8,
        "card_margin": 6,
        "card_padding": 10,
        "row_spacing": 8,
        "header_spacing": 6,
        "toast_seconds": 4,
    }


def _css_px(base: float, scale: float) -> str:
    value = max(8.0, float(base) * float(scale))
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}px"
    return f"{value:.1f}px"


def build_chrome_css(
    theme_name: str | None = "dark",
    density: str | None = "comfortable",
    *,
    ui_font: str | None = None,
    ui_font_scale: float | None = None,
) -> str:
    """Return GTK3 CSS for app chrome. Always non-empty for valid theme names.

    ``ui_font`` is a Pango-style UI face/size (e.g. ``Inter 11`` or ``Sans 12``).
    ``ui_font_scale`` multiplies the derived size ladder (default 1.0).
    """

    resolved = effective_chrome_theme(theme_name)
    tokens = resolve_chrome_tokens(resolved)
    space = spacing_for_density(density)
    pad = space["card_padding"]
    margin = space["card_margin"]
    radius = tokens.card_radius
    dens = normalise_density(density)
    scale = 1.0 if ui_font_scale is None else max(0.75, min(1.75, float(ui_font_scale)))
    if dens == "compact":
        scale *= 0.95

    # Default ladder when no ui_font is set (readable at normal DPI).
    base_size = 13.0
    # Pin a sans-serif UI stack by default so app chrome never inherits the
    # system GTK font (which may be a large monospace face); an explicit
    # ui_font choice below always wins.
    font_family_css = (
        'font-family: "Inter", "SF Pro Text", "Segoe UI", Cantarell, '
        '"Noto Sans", sans-serif;'
    )
    if ui_font:
        family, parsed_size = parse_font_description(ui_font, default="Sans 12")
        # Escape quotes in family for CSS.
        safe_family = family.replace("\\", "\\\\").replace('"', '\\"')
        font_family_css = f'font-family: "{safe_family}", Cantarell, "Noto Sans", sans-serif;'
        # Enforce a readable floor: UI chrome should never go below 12px regardless
        # of what the terminal font size is configured to.
        base_size = max(12.0, min(28.0, parsed_size))

    size = {
        "base": _css_px(base_size, scale),
        "title": _css_px(base_size + 1, scale),
        "muted": _css_px(base_size - 1, scale),
        "chip": _css_px(base_size - 2, scale),
        "mono": _css_px(base_size - 1, scale),
        "button": _css_px(base_size, scale),
        "compact": _css_px(base_size, scale),
    }

    return build_modern_chrome_css(
        tokens=tokens,
        density=dens,
        ui_font=ui_font,
        scale=scale,
        font_family_css=font_family_css,
        size=size,
        pad=pad,
        margin=margin,
        radius=radius,
    )


def terminal_palette_pack(name: str | None) -> dict[str, object]:
    key = normalise_terminal_palette(name)
    return dict(TERMINAL_PALETTE_PACKS[key])


def apply_terminal_palette(theme: TerminalTheme, palette_name: str | None) -> TerminalTheme:
    """Return a TerminalTheme with built-in pack colors applied.

    ``terminator`` keeps the imported Terminator theme unchanged.
    """

    key = normalise_terminal_palette(palette_name)
    if key == "terminator":
        return theme
    pack = TERMINAL_PALETTE_PACKS[key]
    palette = tuple(str(part) for part in pack["palette"])  # type: ignore[index]
    if len(palette) < 16:
        palette = tuple(list(palette) + list(DEFAULT_PALETTE[len(palette) : 16]))
    return replace(
        theme,
        foreground_color=str(pack["foreground"]),
        background_color=str(pack["background"]),
        cursor_color=str(pack.get("cursor") or pack["foreground"]),
        palette=palette[:16],
    )


def kind_for_session(*, name: str = "", command: str = "", tag: str = "") -> str:
    """Classify a session for tab/launcher glyphs."""

    blob = f"{name} {command} {tag}".lower()
    if any(token in blob for token in ("codex", "claude", "opencode", " co ", "llm", "gpt")):
        return "ai"
    if any(token in blob for token in ("docker", "compose", "podman")):
        return "docker"
    if any(token in blob for token in ("ssh ", "ssh\t", "mosh")) or blob.strip().startswith("ssh"):
        return "ssh"
    if any(token in blob for token in ("npm", "node", "yarn", "pnpm", "vite", "next")):
        return "node"
    if any(token in blob for token in ("pytest", "python", "pip", "django", "flask", "uvicorn")):
        return "python"
    if any(token in blob for token in ("pytest", "test", "jest", "vitest", "cargo test")):
        return "test"
    if any(token in blob for token in ("tail -", "journalctl", "log")):
        return "log"
    if any(token in blob for token in ("ipython", "python -i", "node -i", "repl")):
        return "repl"
    if any(token in blob for token in ("bash", "zsh", "fish", "shell", "/bin/sh")):
        return "shell"
    return "task"


def kind_glyph(kind: str | None) -> str:
    return KIND_GLYPHS.get(str(kind or "default"), KIND_GLYPHS["default"])


def health_badge_class(health_label: str | None) -> str:
    text = str(health_label or "").lower()
    if any(token in text for token in ("critical", "error", "kill", "dead", "missing")):
        return "td-badge-danger"
    if any(token in text for token in ("hot", "warn", "attention", "high", "pause")):
        return "td-badge-warn"
    if any(token in text for token in ("ok", "ready", "running", "success", "active")):
        return "td-badge-ok"
    if any(token in text for token in ("input", "wait", "idle", "silent")):
        return "td-badge-info"
    return "td-badge-muted"


def format_resource_cue(cpu_percent: float | None = None, memory_label: str | None = None) -> str:
    parts: list[str] = []
    if cpu_percent is not None:
        parts.append(f"CPU {cpu_percent:.0f}%")
    if memory_label:
        parts.append(memory_label)
    return " · ".join(parts)


def toast_message_for_action(action: str, count: int, *, detail: str = "") -> str:
    action = str(action or "updated").strip()
    noun = "session" if count == 1 else "sessions"
    base = f"{action.capitalize()} {count} {noun}"
    if detail:
        return f"{base} · {detail}"
    return base
