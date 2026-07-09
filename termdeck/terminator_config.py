from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from .models import DEFAULT_PALETTE, TerminalTheme

SECTION_RE = re.compile(r"^\s*(\[+)([^\[\]]+)(\]+)\s*$")
KV_RE = re.compile(r"^\s*([^=#;][^=]*?)\s*=\s*(.*?)\s*$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _to_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clean_colour(value: str | None, default: str | None = None) -> str | None:
    if not value:
        return default
    value = _strip_quotes(value)
    if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
        return value
    return default


def _parse_palette(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_PALETTE
    parts = [_clean_colour(part.strip()) for part in _strip_quotes(value).split(":")]
    colours = tuple(part for part in parts if part)
    if len(colours) >= 8:
        return colours[:16] if len(colours) >= 16 else colours
    return DEFAULT_PALETTE


def _parse_size(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    nums = re.findall(r"\d+", value)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None, None


def _natural_key(value: str) -> tuple[object, ...]:
    parts: list[object] = []
    for part in re.split(r"(\d+)", value):
        if not part:
            continue
        parts.append(int(part) if part.isdigit() else part.casefold())
    return tuple(parts)


def _terminal_command(values: dict[str, str]) -> str:
    custom = values.get("custom_command") or values.get("command") or values.get("commandline")
    use_custom = str(values.get("use_custom_command", "")).strip().lower() in {"1", "true", "yes", "on"}
    if custom and (use_custom or "custom_command" not in values):
        return custom
    if custom and not values.get("use_custom_command"):
        return custom
    return "{shell}"


class TerminatorConfig:
    """Small parser for Terminator's nested INI-like config.

    Terminator uses sections such as [profiles], [[default]], and
    [[[window0]]]. The standard configparser does not understand this nesting,
    so this parser keeps a tuple path for each section.
    """

    def __init__(self, sections: dict[tuple[str, ...], dict[str, str]], source_path: Path | None = None):
        self.sections = sections
        self.source_path = source_path

    @classmethod
    def from_default_path(cls) -> "TerminatorConfig":
        return cls.from_path(Path.home() / ".config" / "terminator" / "config")

    @classmethod
    def from_path(cls, path: str | Path) -> "TerminatorConfig":
        path = Path(path).expanduser()
        if not path.exists():
            return cls({}, path)
        return cls.parse(path.read_text(encoding="utf-8", errors="replace"), path)

    @classmethod
    def parse(cls, text: str, source_path: str | Path | None = None) -> "TerminatorConfig":
        sections: dict[tuple[str, ...], dict[str, str]] = {}
        stack: list[str] = []
        current: tuple[str, ...] = ()

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            section_match = SECTION_RE.match(raw_line)
            if section_match:
                open_brackets, name, close_brackets = section_match.groups()
                level = min(len(open_brackets), len(close_brackets))
                clean_name = name.strip()
                if level <= 1:
                    stack = [clean_name]
                else:
                    stack = stack[: level - 1] + [clean_name]
                current = tuple(stack)
                sections.setdefault(current, {})
                continue

            kv_match = KV_RE.match(raw_line)
            if kv_match and current:
                key, value = kv_match.groups()
                sections.setdefault(current, {})[key.strip()] = _strip_quotes(value)

        return cls(sections, Path(source_path).expanduser() if source_path else None)

    def profile_names(self) -> list[str]:
        names = [path[1] for path in self.sections if len(path) == 2 and path[0] == "profiles"]
        return sorted(set(names))

    def keybindings(self) -> dict[str, str]:
        bindings = dict(self.sections.get(("keybindings",), {}))
        return {k: v for k, v in bindings.items() if v and v.lower() not in {"none", "disabled"}}

    def first_window_size(self) -> tuple[int | None, int | None]:
        for path, values in self.sections.items():
            if len(path) >= 3 and path[0] == "layouts" and values.get("type") == "Window":
                width, height = _parse_size(values.get("size"))
                if width and height:
                    return width, height
        return None, None

    def layout_names(self) -> list[str]:
        names = [path[1] for path in self.sections if len(path) >= 2 and path[0] == "layouts"]
        return sorted(set(names), key=_natural_key)

    def _layout_nodes(self, layout_name: str = "default") -> dict[str, dict[str, str]]:
        names = self.layout_names()
        chosen = layout_name if layout_name in names else (names[0] if names else layout_name)
        return {path[2]: values for path, values in self.sections.items() if len(path) >= 3 and path[0] == "layouts" and path[1] == chosen}

    def _root_window_for_node(self, node_name: str, nodes: dict[str, dict[str, str]], fallback: str) -> str:
        seen: set[str] = set()
        current = node_name
        while current and current not in seen:
            seen.add(current)
            values = nodes.get(current, {})
            if values.get("type") == "Window":
                return current
            parent = values.get("parent", "").strip()
            if not parent or parent not in nodes:
                break
            current = parent
        return fallback

    def to_workspace_dict(self, layout_name: str = "default") -> dict[str, object]:
        """Convert a Terminator layout into a shareable TermDeck workspace recipe.

        Terminator stores panes rather than browser tabs. TermDeck maps each
        Terminal node to a tab in its root Window. Pane geometry is intentionally
        not copied because TermDeck's primary mental model is browser tabs.
        """

        names = self.layout_names()
        chosen = layout_name if layout_name in names else (names[0] if names else layout_name)
        nodes = self._layout_nodes(chosen)
        window_names = [name for name, values in nodes.items() if values.get("type") == "Window"]
        window_names = sorted(window_names, key=_natural_key) or ["window0"]
        first_window = window_names[0]
        grouped: dict[str, list[tuple[str, dict[str, str]]]] = {name: [] for name in window_names}
        for name, values in sorted(nodes.items(), key=lambda item: _natural_key(item[0])):
            if values.get("type") != "Terminal":
                continue
            root = self._root_window_for_node(name, nodes, first_window)
            grouped.setdefault(root, []).append((name, values))
        windows: list[dict[str, object]] = []
        for index, window_name in enumerate(window_names):
            values = nodes.get(window_name, {})
            title = values.get("title") or values.get("name") or ("Terminator" if index == 0 else f"Terminator {index + 1}")
            tabs: list[dict[str, object]] = []
            terminals = grouped.get(window_name, [])
            if not terminals:
                terminals = [("shell", {})]
            for tab_index, (terminal_name, terminal_values) in enumerate(terminals):
                profile = terminal_values.get("profile") or "default"
                tab_title = terminal_values.get("title") or terminal_values.get("name") or (profile if profile != "default" else terminal_name)
                tabs.append({
                    "name": tab_title or f"terminal-{tab_index + 1}",
                    "tag": terminal_values.get("tag") or "terminator",
                    "command": _terminal_command(terminal_values),
                    "cwd": terminal_values.get("directory") or terminal_values.get("working_directory") or terminal_values.get("cwd") or ".",
                    "visible": True,
                })
            windows.append({"title": title, "tabs": tabs})
        return {
            "name": f"terminator-{chosen}",
            "windows": windows,
        }

    def to_workspace_json(self, layout_name: str = "default") -> str:
        return json.dumps(self.to_workspace_dict(layout_name), indent=2, sort_keys=True)

    def to_theme(self, profile_name: str = "default") -> TerminalTheme:
        names = self.profile_names()
        chosen = profile_name if profile_name in names else (names[0] if names else "default")
        profile = self.sections.get(("profiles", chosen), {})
        width, height = self.first_window_size()

        return TerminalTheme(
            font=profile.get("font", "Monospace 12"),
            use_system_font=_to_bool(profile.get("use_system_font", "False")),
            foreground_color=_clean_colour(profile.get("foreground_color"), "#d3d7cf") or "#d3d7cf",
            background_color=_clean_colour(profile.get("background_color"), "#000000") or "#000000",
            palette=_parse_palette(profile.get("palette")),
            cursor_color=_clean_colour(profile.get("cursor_color")),
            cursor_shape=profile.get("cursor_shape", "block").lower(),
            scrollback_lines=_to_int(profile.get("scrollback_lines"), 10000),
            window_width=width,
            window_height=height,
            keybindings=self.keybindings(),
            source_path=self.source_path,
            profile_name=chosen,
        )

    def to_json(self, profile_name: str = "default") -> str:
        theme = self.to_theme(profile_name)
        payload = {
            "profile_name": theme.profile_name,
            "source_path": str(theme.source_path) if theme.source_path else None,
            "font": theme.font,
            "use_system_font": theme.use_system_font,
            "foreground_color": theme.foreground_color,
            "background_color": theme.background_color,
            "palette": list(theme.palette),
            "cursor_color": theme.cursor_color,
            "cursor_shape": theme.cursor_shape,
            "scrollback_lines": theme.scrollback_lines,
            "window_width": theme.window_width,
            "window_height": theme.window_height,
            "keybindings": dict(theme.keybindings),
        }
        return json.dumps(payload, indent=2, sort_keys=True)


def load_terminator_theme(path: str | Path | None = None, profile_name: str = "default") -> TerminalTheme:
    config = TerminatorConfig.from_path(path) if path else TerminatorConfig.from_default_path()
    return config.to_theme(profile_name)
