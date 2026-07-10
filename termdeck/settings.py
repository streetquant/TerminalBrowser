from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .health import HealthPolicy
from .models import TerminalTheme
from .resource_budget import BudgetConfigError, parse_memory
from .shortcuts import shortcut_profile_names
from .ui_theme import (
    apply_terminal_overrides,
    chrome_theme_names,
    cursor_shape_names,
    density_names,
    normalise_chrome_theme,
    normalise_cursor_shape,
    normalise_density,
    normalise_font_description,
    normalise_terminal_palette,
    terminal_palette_names,
)

CONFIG_DIR = Path.home() / ".config" / "termdeck"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.json"


class SettingsError(ValueError):
    """Raised when the local TermDeck settings file is invalid."""


def _number(row: Mapping[str, Any], *names: str, default: float) -> float:
    for name in names:
        if name in row and row[name] not in {None, ""}:
            try:
                value = float(row[name])
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{name} must be a number") from exc
            if value < 0:
                raise SettingsError(f"{name} must be non-negative")
            return value
    return default


def _integer(row: Mapping[str, Any], *names: str, default: int) -> int:
    value = _number(row, *names, default=float(default))
    return int(value)


def _boolean(row: Mapping[str, Any], *names: str, default: bool) -> bool:
    for name in names:
        if name in row and row[name] not in {None, ""}:
            value = row[name]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                cleaned = value.strip().lower()
                if cleaned in {"1", "true", "yes", "on"}:
                    return True
                if cleaned in {"0", "false", "no", "off"}:
                    return False
            raise SettingsError(f"{name} must be a boolean")
    return default


def normalise_side_pane_position(value: object | None) -> str:
    cleaned = str(value or "right").strip().lower().replace("-", "_")
    aliases = {
        "left": "left",
        "start": "left",
        "right": "right",
        "end": "right",
    }
    if cleaned not in aliases:
        raise SettingsError("appearance.side_pane_position must be left or right")
    return aliases[cleaned]



def _memory(row: Mapping[str, Any], *names: str, default: int) -> int:
    for name in names:
        if name in row and row[name] not in {None, ""}:
            try:
                parsed = parse_memory(row[name])
            except BudgetConfigError as exc:
                raise SettingsError(str(exc)) from exc
            if parsed is None:
                return default
            return parsed
    return default


def _policy_from_mapping(row: Mapping[str, Any] | None, *, base: HealthPolicy | None = None) -> HealthPolicy:
    if row is None:
        return base or HealthPolicy()
    if not isinstance(row, Mapping):
        raise SettingsError("health policy must be an object")
    base = base or HealthPolicy()
    return HealthPolicy(
        high_cpu_percent=_number(row, "high_cpu_percent", "cpu_warn_percent", "cpu_warn", default=base.high_cpu_percent),
        critical_cpu_percent=_number(row, "critical_cpu_percent", "cpu_critical_percent", "cpu_critical", default=base.critical_cpu_percent),
        high_memory_bytes=_memory(row, "high_memory", "high_memory_bytes", "memory_warn", "memory_warn_bytes", default=base.high_memory_bytes),
        critical_memory_bytes=_memory(row, "critical_memory", "critical_memory_bytes", "memory_critical", "memory_critical_bytes", default=base.critical_memory_bytes),
        process_count_warn=_integer(row, "process_count_warn", "processes_warn", "max_processes_warn", default=base.process_count_warn),
    )


def _policy_dict(policy: HealthPolicy) -> dict[str, object]:
    return {
        "high_cpu_percent": policy.high_cpu_percent,
        "critical_cpu_percent": policy.critical_cpu_percent,
        "high_memory_bytes": policy.high_memory_bytes,
        "critical_memory_bytes": policy.critical_memory_bytes,
        "process_count_warn": policy.process_count_warn,
    }


@dataclass(frozen=True)
class AppSettings:
    """Small local settings model for resource-health policies.

    The first public config surface is intentionally narrow: global health
    thresholds and optional per-tag overrides.  It keeps diagnostics and the
    master task table deterministic without turning the MVP into a large
    preferences system.
    """

    path: Path = DEFAULT_CONFIG_PATH
    default_policy: HealthPolicy = field(default_factory=HealthPolicy)
    tag_policies: Mapping[str, HealthPolicy] = field(default_factory=dict)
    keyboard_profile: str = "browser"
    import_terminator_keybindings: bool = True
    chrome_theme: str = "dark"
    terminal_palette: str = "terminator"
    ui_density: str = "comfortable"
    tabs_pane_visible: bool = False
    ui_refresh_interval_seconds: float = 5.0
    # Interface (non-terminal) type. None = GTK default family + TermDeck size ladder.
    ui_font: str | None = None
    ui_font_scale: float | None = None
    # Minimal VTE options. None means "leave Terminator / palette pack value".
    terminal_font: str | None = None
    terminal_use_system_font: bool | None = None
    terminal_cursor_shape: str | None = None
    terminal_cursor_color: str | None = None
    terminal_cursor_blink: bool | None = None
    terminal_scrollback_lines: int | None = None
    side_pane_position: str = "right"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppSettings":
        resolved = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        if not resolved.exists():
            return cls(path=resolved)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SettingsError(f"invalid JSON in settings file: {resolved}") from exc
        if not isinstance(payload, Mapping):
            raise SettingsError("settings file must be a JSON object")
        default_policy = _policy_from_mapping(payload.get("health") if isinstance(payload.get("health"), Mapping) else None)
        tag_rows = payload.get("tags", payload.get("tag_policies", {}))
        if tag_rows is None:
            tag_rows = {}
        if not isinstance(tag_rows, Mapping):
            raise SettingsError("tags must be an object mapping tag names to policies")
        tag_policies = {str(tag): _policy_from_mapping(row, base=default_policy) for tag, row in tag_rows.items()}
        keyboard = payload.get("keyboard", {})
        if keyboard is None:
            keyboard = {}
        if not isinstance(keyboard, Mapping):
            raise SettingsError("keyboard must be an object")
        keyboard_profile = str(keyboard.get("profile", "browser"))
        if keyboard_profile not in shortcut_profile_names():
            allowed = ", ".join(shortcut_profile_names())
            raise SettingsError(f"keyboard.profile must be one of: {allowed}")
        import_terminator_keybindings = _boolean(keyboard, "import_terminator_keybindings", "import_terminator", default=True)

        appearance = payload.get("appearance", payload.get("ui", {}))
        if appearance is None:
            appearance = {}
        if not isinstance(appearance, Mapping):
            raise SettingsError("appearance must be an object")
        chrome_theme = normalise_chrome_theme(
            appearance.get("chrome_theme", appearance.get("theme", payload.get("chrome_theme", "dark")))
        )
        if chrome_theme not in chrome_theme_names():
            raise SettingsError(f"appearance.chrome_theme must be one of: {', '.join(chrome_theme_names())}")
        terminal_palette = normalise_terminal_palette(
            appearance.get("terminal_palette", appearance.get("palette", payload.get("terminal_palette", "terminator")))
        )
        if terminal_palette not in terminal_palette_names():
            raise SettingsError(f"appearance.terminal_palette must be one of: {', '.join(terminal_palette_names())}")
        ui_density = normalise_density(appearance.get("density", appearance.get("ui_density", "comfortable")))
        if ui_density not in density_names():
            raise SettingsError(f"appearance.density must be one of: {', '.join(density_names())}")
        if "tabs_pane_visible" in appearance or "show_tabs_pane" in appearance:
            tabs_pane_visible = _boolean(appearance, "tabs_pane_visible", "show_tabs_pane", default=False)
        elif "focus_mode" in appearance or "focus" in appearance:
            # Backward compatibility: the old focus flag hid the pane when true.
            tabs_pane_visible = not _boolean(appearance, "focus_mode", "focus", default=False)
        else:
            tabs_pane_visible = False
        ui_refresh_interval_seconds = _number(
            appearance,
            "refresh_interval_seconds",
            "ui_refresh_interval_seconds",
            "refresh_seconds",
            default=5.0,
        )
        ui_refresh_interval_seconds = max(1.0, min(30.0, ui_refresh_interval_seconds))
        side_pane_position = normalise_side_pane_position(
            appearance.get("side_pane_position", appearance.get("side", appearance.get("sessions_pane", "right")))
        )

        ui_font = None
        if "ui_font" in appearance and appearance.get("ui_font") not in {None, ""}:
            ui_font = normalise_font_description(str(appearance.get("ui_font")), default="Sans 11")
        ui_font_scale = None
        if "ui_font_scale" in appearance and appearance.get("ui_font_scale") not in {None, ""}:
            try:
                ui_font_scale = float(appearance.get("ui_font_scale"))  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise SettingsError("appearance.ui_font_scale must be a number") from exc
            if ui_font_scale <= 0:
                raise SettingsError("appearance.ui_font_scale must be positive")
            ui_font_scale = max(0.75, min(1.75, ui_font_scale))

        terminal = appearance.get("terminal", payload.get("terminal", {}))
        if terminal is None:
            terminal = {}
        if not isinstance(terminal, Mapping):
            raise SettingsError("appearance.terminal must be an object")
        terminal_font = None
        if "font" in terminal and terminal.get("font") not in {None, ""}:
            terminal_font = normalise_font_description(str(terminal.get("font")))
        terminal_use_system_font = None
        if "use_system_font" in terminal:
            terminal_use_system_font = _boolean(terminal, "use_system_font", default=False)
        terminal_cursor_shape = None
        if "cursor_shape" in terminal and terminal.get("cursor_shape") not in {None, ""}:
            terminal_cursor_shape = normalise_cursor_shape(str(terminal.get("cursor_shape")))
            if terminal_cursor_shape not in cursor_shape_names():
                raise SettingsError(
                    f"appearance.terminal.cursor_shape must be one of: {', '.join(cursor_shape_names())}"
                )
        terminal_cursor_color = None
        if "cursor_color" in terminal and terminal.get("cursor_color") not in {None, ""}:
            terminal_cursor_color = str(terminal.get("cursor_color")).strip()
        terminal_cursor_blink = None
        if "cursor_blink" in terminal or "cursor_blinking" in terminal:
            terminal_cursor_blink = _boolean(terminal, "cursor_blink", "cursor_blinking", default=True)
        terminal_scrollback_lines = None
        if "scrollback_lines" in terminal and terminal.get("scrollback_lines") not in {None, ""}:
            terminal_scrollback_lines = max(100, _integer(terminal, "scrollback_lines", default=10000))

        return cls(
            path=resolved,
            default_policy=default_policy,
            tag_policies=tag_policies,
            keyboard_profile=keyboard_profile,
            import_terminator_keybindings=import_terminator_keybindings,
            chrome_theme=chrome_theme,
            terminal_palette=terminal_palette,
            ui_density=ui_density,
            tabs_pane_visible=tabs_pane_visible,
            ui_refresh_interval_seconds=ui_refresh_interval_seconds,
            ui_font=ui_font,
            ui_font_scale=ui_font_scale,
            terminal_font=terminal_font,
            terminal_use_system_font=terminal_use_system_font,
            terminal_cursor_shape=terminal_cursor_shape,
            terminal_cursor_color=terminal_cursor_color,
            terminal_cursor_blink=terminal_cursor_blink,
            terminal_scrollback_lines=terminal_scrollback_lines,
            side_pane_position=side_pane_position,
        )

    def policy_for_record(self, record) -> HealthPolicy:
        return self.tag_policies.get(getattr(record, "tag", ""), self.default_policy)

    def terminal_options_dict(self) -> dict[str, object]:
        """Serialize the minimal terminal options block for samples / save."""

        row: dict[str, object] = {}
        if self.terminal_font is not None:
            row["font"] = self.terminal_font
        if self.terminal_use_system_font is not None:
            row["use_system_font"] = self.terminal_use_system_font
        if self.terminal_cursor_shape is not None:
            row["cursor_shape"] = self.terminal_cursor_shape
        if self.terminal_cursor_color is not None:
            row["cursor_color"] = self.terminal_cursor_color
        if self.terminal_cursor_blink is not None:
            row["cursor_blink"] = self.terminal_cursor_blink
        if self.terminal_scrollback_lines is not None:
            row["scrollback_lines"] = self.terminal_scrollback_lines
        return row

    def apply_to_theme(self, theme: TerminalTheme) -> TerminalTheme:
        """Apply palette-independent terminal overrides to a theme object."""

        return apply_terminal_overrides(
            theme,
            font=self.terminal_font,
            use_system_font=self.terminal_use_system_font,
            cursor_shape=self.terminal_cursor_shape,
            cursor_color=self.terminal_cursor_color,
            cursor_blink=self.terminal_cursor_blink,
            scrollback_lines=self.terminal_scrollback_lines,
        )

    def with_terminal_options(
        self,
        *,
        font: str | None = None,
        use_system_font: bool | None = None,
        cursor_shape: str | None = None,
        cursor_color: str | None = None,
        cursor_blink: bool | None = None,
        scrollback_lines: int | None = None,
        terminal_palette: str | None = None,
    ) -> "AppSettings":
        return replace(
            self,
            terminal_font=normalise_font_description(font) if font is not None else self.terminal_font,
            terminal_use_system_font=use_system_font if use_system_font is not None else self.terminal_use_system_font,
            terminal_cursor_shape=normalise_cursor_shape(cursor_shape) if cursor_shape is not None else self.terminal_cursor_shape,
            terminal_cursor_color=cursor_color if cursor_color is not None else self.terminal_cursor_color,
            terminal_cursor_blink=bool(cursor_blink) if cursor_blink is not None else self.terminal_cursor_blink,
            terminal_scrollback_lines=scrollback_lines if scrollback_lines is not None else self.terminal_scrollback_lines,
            terminal_palette=normalise_terminal_palette(terminal_palette) if terminal_palette is not None else self.terminal_palette,
        )

    def budget_summary(self) -> dict[str, object]:
        appearance: dict[str, object] = {
            "chrome_theme": self.chrome_theme,
            "terminal_palette": self.terminal_palette,
            "density": self.ui_density,
            "tabs_pane_visible": self.tabs_pane_visible,
            "refresh_interval_seconds": self.ui_refresh_interval_seconds,
            "side_pane_position": self.side_pane_position,
        }
        if self.ui_font is not None:
            appearance["ui_font"] = self.ui_font
        if self.ui_font_scale is not None:
            appearance["ui_font_scale"] = self.ui_font_scale
        terminal = self.terminal_options_dict()
        if terminal:
            appearance["terminal"] = terminal
        return {
            "path": str(self.path),
            "default_policy": _policy_dict(self.default_policy),
            "tag_policies": {tag: _policy_dict(policy) for tag, policy in sorted(self.tag_policies.items())},
            "keyboard": {
                "profile": self.keyboard_profile,
                "import_terminator_keybindings": self.import_terminator_keybindings,
            },
            "appearance": appearance,
        }


def merge_settings_payload(path: Path, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Load existing config JSON (if any), deep-merge updates, and return the payload."""

    payload: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SettingsError(f"invalid JSON in settings file: {path}") from exc
        if not isinstance(loaded, Mapping):
            raise SettingsError("settings file must be a JSON object")
        payload = dict(loaded)

    def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in patch.items():
            if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
                out[key] = _merge(dict(out[key]), value)
            else:
                out[key] = value
        return out

    return _merge(payload, updates)


def save_settings_payload(path: Path, payload: Mapping[str, Any]) -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def save_terminal_options(
    path: Path | None = None,
    *,
    font: str | None = None,
    use_system_font: bool | None = None,
    cursor_shape: str | None = None,
    cursor_color: str | None = None,
    cursor_blink: bool | None = None,
    scrollback_lines: int | None = None,
    terminal_palette: str | None = None,
    chrome_theme: str | None = None,
    density: str | None = None,
    ui_font: str | None = None,
    ui_font_scale: float | None = None,
    side_pane_position: str | None = None,
    tabs_pane_visible: bool | None = None,
) -> Path:
    """Persist minimal appearance/terminal options into the user config file."""

    resolved = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    terminal: dict[str, Any] = {}
    if font is not None:
        terminal["font"] = normalise_font_description(font)
    if use_system_font is not None:
        terminal["use_system_font"] = bool(use_system_font)
    if cursor_shape is not None:
        terminal["cursor_shape"] = normalise_cursor_shape(cursor_shape)
    if cursor_color is not None:
        terminal["cursor_color"] = str(cursor_color).strip()
    if cursor_blink is not None:
        terminal["cursor_blink"] = bool(cursor_blink)
    if scrollback_lines is not None:
        terminal["scrollback_lines"] = max(100, int(scrollback_lines))

    appearance: dict[str, Any] = {}
    if terminal_palette is not None:
        appearance["terminal_palette"] = normalise_terminal_palette(terminal_palette)
    if chrome_theme is not None:
        appearance["chrome_theme"] = normalise_chrome_theme(chrome_theme)
    if density is not None:
        appearance["density"] = normalise_density(density)
    if side_pane_position is not None:
        appearance["side_pane_position"] = normalise_side_pane_position(side_pane_position)
    if tabs_pane_visible is not None:
        appearance["tabs_pane_visible"] = bool(tabs_pane_visible)
    if ui_font is not None:
        cleaned = str(ui_font).strip()
        appearance["ui_font"] = normalise_font_description(cleaned, default="Sans 11") if cleaned else ""
    if ui_font_scale is not None:
        appearance["ui_font_scale"] = max(0.75, min(1.75, float(ui_font_scale)))
    if terminal:
        appearance["terminal"] = terminal
    payload = merge_settings_payload(resolved, {"appearance": appearance})
    # Empty ui_font means "use system" — drop the key if blank after merge.
    app = payload.get("appearance")
    if isinstance(app, dict):
        if cursor_blink is not None and isinstance(app.get("terminal"), dict):
            app["terminal"].pop("cursor_blinking", None)
        if tabs_pane_visible is not None:
            app.pop("focus_mode", None)
            app.pop("focus", None)
        if app.get("ui_font") == "":
            app.pop("ui_font", None)
    return save_settings_payload(resolved, payload)


def sample_settings_json() -> str:
    payload = {
        "keyboard": {
            "profile": "browser",
            "import_terminator_keybindings": True,
        },
        "appearance": {
            "chrome_theme": "dark",
            "terminal_palette": "catppuccin",
            "density": "comfortable",
            "tabs_pane_visible": False,
            "refresh_interval_seconds": 5.0,
            "side_pane_position": "right",
            "ui_font": "Sans 11",
            "ui_font_scale": 1.0,
            "terminal": {
                "font": "Monospace 13",
                "use_system_font": False,
                "cursor_shape": "block",
                "cursor_color": "#cdd6f4",
                "cursor_blink": False,
                "scrollback_lines": 10000,
            },
        },
        "health": {
            "high_cpu_percent": 150,
            "critical_cpu_percent": 300,
            "high_memory": "2GiB",
            "critical_memory": "4GiB",
            "process_count_warn": 48,
        },
        "tags": {
            "ai": {
                "high_cpu_percent": 250,
                "critical_cpu_percent": 500,
                "high_memory": "6GiB",
                "critical_memory": "10GiB",
                "process_count_warn": 128,
            }
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)
