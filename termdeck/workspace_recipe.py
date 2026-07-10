from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .command_render import render_command


class WorkspaceRecipeError(ValueError):
    """Raised when a TermDeck workspace recipe is invalid."""


_SAFE = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class RecipeTab:
    name: str
    tag: str
    command: str
    cwd: str
    visible: bool = True
    pinned: bool = False


@dataclass(frozen=True)
class RecipeWindow:
    title: str
    tabs: tuple[RecipeTab, ...]


@dataclass(frozen=True)
class WorkspaceRecipe:
    name: str
    windows: tuple[RecipeWindow, ...]
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def _slug(value: str, fallback: str = "workspace") -> str:
    cleaned = _SAFE.sub("-", value.strip().lower()).strip("-")
    return cleaned or fallback


def _string(value: Any, field: str, *, default: str | None = None) -> str:
    if value is None:
        if default is not None:
            return default
        raise WorkspaceRecipeError(f"missing required field: {field}")
    if not isinstance(value, str):
        raise WorkspaceRecipeError(f"{field} must be a string")
    value = value.strip()
    if not value and default is None:
        raise WorkspaceRecipeError(f"{field} cannot be empty")
    return value or (default or "")


def _bool(value: Any, field: str, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise WorkspaceRecipeError(f"{field} must be true or false")


def _normalise_cwd(value: Any, base_dir: Path) -> str:
    raw = _string(value, "cwd", default=str(base_dir))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve(strict=False))


def _parse_tab(item: Any, *, index: int, base_dir: Path) -> RecipeTab:
    if not isinstance(item, dict):
        raise WorkspaceRecipeError(f"tabs[{index}] must be an object")
    raw_command = _string(item.get("command"), f"tabs[{index}].command")
    tag = _string(item.get("tag"), f"tabs[{index}].tag", default="default")
    cwd = _normalise_cwd(item.get("cwd"), base_dir)
    command = render_command(raw_command, cwd=cwd)
    name = _string(item.get("name"), f"tabs[{index}].name", default=command.split()[0] if command.split() else "task")
    visible = _bool(item.get("visible"), f"tabs[{index}].visible", default=True)
    pinned = _bool(item.get("pinned"), f"tabs[{index}].pinned", default=False)
    return RecipeTab(name=name, tag=tag, command=command, cwd=cwd, visible=visible, pinned=pinned)


def _parse_tabs(items: Any, *, base_dir: Path, context: str) -> tuple[RecipeTab, ...]:
    if not isinstance(items, list) or not items:
        raise WorkspaceRecipeError(f"{context} must contain at least one tab")
    return tuple(_parse_tab(item, index=index, base_dir=base_dir) for index, item in enumerate(items))


def _parse_window(item: Any, *, index: int, base_dir: Path) -> RecipeWindow:
    if not isinstance(item, dict):
        raise WorkspaceRecipeError(f"windows[{index}] must be an object")
    title = _string(item.get("title"), f"windows[{index}].title", default=f"Window {index + 1}")
    tabs = _parse_tabs(item.get("tabs"), base_dir=base_dir, context=f"windows[{index}].tabs")
    return RecipeWindow(title=title, tabs=tabs)


def parse_recipe(data: dict[str, Any], *, base_dir: str | Path | None = None, source_path: str | Path | None = None) -> WorkspaceRecipe:
    """Validate and normalise a JSON workspace recipe.

    The supported shape is intentionally small and dependency-free::

        {
          "name": "demo",
          "windows": [
            {"title": "Dev", "tabs": [{"name": "api", "command": "python -m http.server"}]}
          ]
        }

    For convenience, a top-level ``tabs`` array is accepted and converted into
    one window titled ``Main``.
    """

    if not isinstance(data, dict):
        raise WorkspaceRecipeError("recipe root must be an object")
    base = Path(base_dir or (Path(source_path).expanduser().parent if source_path else Path.cwd())).resolve(strict=False)
    name = _string(data.get("name"), "name", default="workspace")

    raw_windows = data.get("windows")
    if raw_windows is None and "tabs" in data:
        raw_windows = [{"title": data.get("title", "Main"), "tabs": data["tabs"]}]
    if not isinstance(raw_windows, list) or not raw_windows:
        raise WorkspaceRecipeError("recipe must contain a non-empty windows array or top-level tabs array")

    windows = tuple(_parse_window(item, index=index, base_dir=base) for index, item in enumerate(raw_windows))
    return WorkspaceRecipe(name=name, windows=windows, source_path=str(source_path) if source_path else None)


def load_recipe(path: str | Path) -> WorkspaceRecipe:
    recipe_path = Path(path).expanduser().resolve(strict=False)
    try:
        data = json.loads(recipe_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceRecipeError(f"workspace recipe not found: {recipe_path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceRecipeError(f"invalid JSON in workspace recipe: {exc}") from exc
    return parse_recipe(data, base_dir=recipe_path.parent, source_path=recipe_path)


def recipe_summary_rows(recipe: WorkspaceRecipe) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for window_index, window in enumerate(recipe.windows, start=1):
        for tab_index, tab in enumerate(window.tabs, start=1):
            rows.append(
                {
                    "workspace": recipe.name,
                    "window_index": window_index,
                    "window": window.title,
                    "tab_index": tab_index,
                    "name": tab.name,
                    "tag": tab.tag,
                    "command": tab.command,
                    "cwd": tab.cwd,
                    "visible": tab.visible,
                    "pinned": tab.pinned,
                    "session_hint": f"{_slug(recipe.name)}-{window_index:02d}-{tab_index:02d}",
                }
            )
    return rows


def recipe_to_json(recipe: WorkspaceRecipe) -> str:
    return json.dumps(recipe.to_dict(), indent=2, sort_keys=True)


def recipe_summary_json(recipe: WorkspaceRecipe) -> str:
    return json.dumps(recipe_summary_rows(recipe), indent=2, sort_keys=True)


SAMPLE_RECIPE = {
    "name": "sample-dev-workspace",
    "windows": [
        {
            "title": "Development",
            "tabs": [
                {"name": "shell", "tag": "default", "command": "{shell}", "pinned": True},
                {"name": "api", "tag": "dev", "command": "python3 -m http.server 8000"},
                {"name": "tests", "tag": "tests", "command": "pytest"},
            ],
        },
        {
            "title": "Agents",
            "tabs": [
                {"name": "codex", "tag": "ai", "command": "codex"},
            ],
        },
    ],
}


def sample_recipe_json() -> str:
    return json.dumps(SAMPLE_RECIPE, indent=2, sort_keys=True)


def recipe_from_sessions(
    records: tuple["SessionRecord", ...] | list["SessionRecord"],
    *,
    name: str = "termdeck-export",
    visible_only: bool = False,
) -> WorkspaceRecipe:
    """Create a shareable workspace recipe from remembered sessions.

    This is the reverse of loading a workspace recipe: users can arrange a set
    of terminal tabs, export them, commit the recipe to a project, and later
    reopen the same browser-like window/tab layout.
    """

    from .models import SessionRecord  # local import avoids a module cycle at import time

    if not isinstance(records, (list, tuple)):
        records = list(records)  # type: ignore[assignment]
    filtered: list[SessionRecord] = [record for record in records if (record.visible or not visible_only)]
    if not filtered:
        raise WorkspaceRecipeError("cannot export an empty session list")

    grouped: dict[str, list[SessionRecord]] = {}
    titles: dict[str, str] = {}
    first_index: dict[str, int] = {}
    for order, record in enumerate(filtered):
        grouped.setdefault(record.window_id, []).append(record)
        titles.setdefault(record.window_id, record.window_title or "Window")
        first_index.setdefault(record.window_id, order)

    windows: list[RecipeWindow] = []
    for window_id in sorted(grouped, key=lambda wid: (first_index[wid], titles.get(wid, ""), wid)):
        rows = sorted(grouped[window_id], key=lambda row: (row.tab_index, row.last_opened_at, row.id))
        tabs = tuple(
            RecipeTab(
                name=record.name,
                tag=record.tag,
                command=record.command,
                cwd=record.cwd,
                visible=record.visible,
                pinned=record.pinned,
            )
            for record in rows
        )
        windows.append(RecipeWindow(title=titles.get(window_id, "Window"), tabs=tabs))
    return WorkspaceRecipe(name=name or "termdeck-export", windows=tuple(windows))


def recipe_export_json(
    records: tuple["SessionRecord", ...] | list["SessionRecord"],
    *,
    name: str = "termdeck-export",
    visible_only: bool = False,
) -> str:
    return recipe_to_json(recipe_from_sessions(records, name=name, visible_only=visible_only))
