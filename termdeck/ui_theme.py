"""Modern aesthetic tokens, CSS chrome, and built-in VTE palette packs.

Pure helpers stay display-free so unit tests can cover settings and style
payloads without opening a GTK window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from .chrome_css import build_modern_chrome_css
from .models import DEFAULT_PALETTE, TerminalTheme

CHROME_THEME_NAMES = ("dark", "noir", "system")
DENSITY_NAMES = ("comfortable", "compact")
CURSOR_SHAPE_NAMES = ("block", "underline", "ibeam")
TERMINAL_PALETTE_NAMES = (
    "terminator",
    "catppuccin",
    "tokyo-night",
    "nord",
    "one-dark",
    "solarized-dark",
)

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
    card_radius: int = 10
    mono: str = "Monospace"


CHROME_TOKENS: Mapping[str, ChromeTokens] = {
    "dark": ChromeTokens(
        name="dark",
        # Neutral graphite surfaces with a single restrained blue accent —
        # flat and professional, in the spirit of mature terminal apps.
        bg="#16181d",
        bg_raised="#1d2026",
        bg_sunken="#101216",
        fg="#dde1e8",
        fg_muted="#8b93a1",
        border="#2b2f37",
        accent="#5b8cff",
        accent_fg="#ffffff",
        danger="#ef5f74",
        warning="#e0b56a",
        success="#43cf9c",
        card_radius=8,
    ),
    "noir": ChromeTokens(
        name="noir",
        # Pure charcoal with a soft emerald accent — high contrast, no blue tint
        bg="#0e0f10",
        bg_raised="#161819",
        bg_sunken="#0a0b0b",
        fg="#e8eaec",
        fg_muted="#8f959b",
        border="#26292c",
        accent="#34d399",
        accent_fg="#04120b",
        danger="#f2556c",
        warning="#e3b341",
        success="#34d399",
        card_radius=8,
    ),
    "system": ChromeTokens(
        name="system",
        # Clean neutral light shell — follows desktop when system prefers light.
        # When system prefers dark, effective_chrome_theme() maps to "dark".
        bg="#f6f7f9",
        bg_raised="#ffffff",
        bg_sunken="#eceef2",
        fg="#1c2128",
        fg_muted="#59626e",
        border="#d7dbe1",
        accent="#2f6fed",
        accent_fg="#ffffff",
        danger="#c62b41",
        warning="#8a5a00",
        success="#0a6e4a",
        card_radius=8,
    ),
}


# Named 16-color VTE packs + fg/bg. Values are modern community palettes.
TERMINAL_PALETTE_PACKS: Mapping[str, dict[str, object]] = {
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
            "#282c34",
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
            "#002b36",
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
    cleaned = str(name or "terminator").strip().lower().replace("_", "-")
    aliases = {
        "default": "terminator",
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
        return "terminator"
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
    min_size: float = 6.0,
    max_size: float = 72.0,
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
    the light ``system`` token pack. Explicit dark/noir are unchanged.
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
    font_family_css = ""
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
        "button": _css_px(base_size - 1, scale),
        "compact": _css_px(base_size - 1, scale),
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
