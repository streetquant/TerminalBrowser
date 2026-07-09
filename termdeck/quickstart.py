from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .desktop_integration import build_install_plan
from .project_detect import detect_project
from .terminator_config import TerminatorConfig
from .workspace_recipe import recipe_summary_rows

INSTALL_HINT = (
    "Debian/Ubuntu: sudo apt install tmux python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 | "
    "Arch: sudo pacman -S tmux python-gobject gtk3 vte3"
)


def _has_gi_namespace(namespace: str, version: str) -> bool:
    try:
        import gi  # type: ignore

        gi.require_version(namespace, version)
        __import__("gi.repository", fromlist=[namespace])
        return True
    except Exception:
        return False


def _dependency_status() -> dict[str, dict[str, object]]:
    tmux_path = shutil.which("tmux")
    gdk_ok = _has_gi_namespace("Gdk", "3.0")
    gtk_ok = _has_gi_namespace("Gtk", "3.0")
    vte_ok = _has_gi_namespace("Vte", "2.91")
    return {
        "tmux": {
            "ok": bool(tmux_path),
            "detail": tmux_path or "missing",
            "required_for": "persistent terminal task sessions",
        },
        "gdk3": {
            "ok": gdk_ok,
            "detail": "ok" if gdk_ok else "missing",
            "required_for": "GTK3 windowing (must be pinned before Gtk on dual Gdk systems)",
        },
        "gtk3": {
            "ok": gtk_ok,
            "detail": "ok" if gtk_ok else "missing",
            "required_for": "native browser-like window and side pane",
        },
        "vte": {
            "ok": vte_ok,
            "detail": "ok" if vte_ok else "missing",
            "required_for": "lightweight terminal rendering",
        },
    }


def _terminator_summary(path: str | Path | None = None) -> dict[str, object]:
    config = TerminatorConfig.from_path(path) if path else TerminatorConfig.from_default_path()
    theme = config.to_theme()
    source = config.source_path
    found = bool(source and source.exists())
    return {
        "found": found,
        "source_path": str(source) if source else None,
        "profile": theme.profile_name,
        "font": theme.font,
        "use_system_font": theme.use_system_font,
        "foreground_color": theme.foreground_color,
        "background_color": theme.background_color,
        "cursor_shape": theme.cursor_shape,
        "scrollback_lines": theme.scrollback_lines,
        "keybindings": len(theme.keybindings),
        "layouts": config.layout_names(),
    }


def _desktop_summary(*, data_home: str | Path | None = None, command: str | None = None) -> dict[str, object]:
    plan = build_install_plan(command=command, data_home=data_home)
    return {
        "installed": plan.paths.desktop_file.exists() and plan.paths.icon_file.exists(),
        "desktop_file": str(plan.paths.desktop_file),
        "icon_file": str(plan.paths.icon_file),
        "launch_command": plan.command,
    }


def _project_summary(path: str | Path | None, *, include_ai: bool) -> dict[str, object]:
    detection = detect_project(path, include_ai=include_ai)
    rows = recipe_summary_rows(detection.recipe)
    return {
        "root": detection.root,
        "name": detection.name,
        "notes": [note.to_dict() for note in detection.notes],
        "window_count": len(detection.recipe.windows),
        "tab_count": len(rows),
        "tabs": [
            {
                "window": row["window"],
                "name": row["name"],
                "tag": row["tag"],
                "command": row["command"],
                "visible": row["visible"],
            }
            for row in rows
        ],
    }


def _action(action_id: str, label: str, command: str, why: str) -> dict[str, str]:
    return {"id": action_id, "label": label, "command": command, "why": why}


def _recommended_actions(payload: dict[str, Any], *, path: Path, include_ai: bool) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    dependencies = payload["dependencies"]
    if not all(item["ok"] for item in dependencies.values()):
        actions.append(_action(
            "install-dependencies",
            "Install lightweight runtime dependencies",
            INSTALL_HINT,
            "TermDeck needs tmux for persistence and GTK/VTE for the native lightweight UI.",
        ))

    project = payload["project"]
    actions.append(_action(
        "write-project-workspace",
        "Create a project workspace recipe",
        "python3 -m termdeck project-init " + shlex.quote(str(path)) + (" --include-ai" if include_ai else ""),
        f"Detected {project['tab_count']} useful tab(s) for this directory.",
    ))
    actions.append(_action(
        "preview-project-workspace",
        "Preview the detected workspace without starting processes",
        "python3 -m termdeck project-detect " + shlex.quote(str(path)),
        "Shows the detected browser windows and tabs before writing a recipe.",
    ))

    terminator = payload["terminator"]
    if terminator["found"]:
        actions.append(_action(
            "inspect-terminator-theme",
            "Inspect imported Terminator look and shortcuts",
            "python3 -m termdeck parse-terminator",
            "Confirms the font, colours, scrollback, and keybindings TermDeck will reuse.",
        ))
        if terminator["layouts"]:
            actions.append(_action(
                "convert-terminator-layout",
                "Convert a Terminator layout into TermDeck windows and tabs",
                "python3 -m termdeck terminator-workspace --output terminator.workspace.json",
                "Migrates existing Terminator terminal nodes into browser-style tabs.",
            ))

    desktop = payload["desktop"]
    if not desktop["installed"]:
        actions.append(_action(
            "install-app-launcher",
            "Install browser-like app launcher",
            "python3 -m termdeck install-desktop",
            "Lets users start TermDeck from the desktop/app menu instead of typing a command.",
        ))

    actions.append(_action(
        "launch-gui",
        "Launch TermDeck",
        "python3 -m termdeck gui",
        "Opens the browser-like terminal task manager.",
    ))
    return actions


def build_quickstart(
    path: str | Path | None = None,
    *,
    include_ai: bool = False,
    terminator_path: str | Path | None = None,
    data_home: str | Path | None = None,
    desktop_command: str | None = None,
) -> dict[str, object]:
    root = Path(path or Path.cwd()).expanduser().resolve(strict=False)
    if root.is_file():
        root = root.parent
    payload: dict[str, Any] = {
        "schema": "termdeck.quickstart",
        "version": __version__,
        "root": str(root),
        "dependencies": _dependency_status(),
        "terminator": _terminator_summary(terminator_path),
        "desktop": _desktop_summary(data_home=data_home, command=desktop_command),
        "project": _project_summary(root, include_ai=include_ai),
    }
    payload["ready_for_gui"] = bool(payload["dependencies"]["tmux"]["ok"] and payload["dependencies"]["gtk3"]["ok"] and payload["dependencies"]["vte"]["ok"])
    payload["recommended_actions"] = _recommended_actions(payload, path=root, include_ai=include_ai)
    return payload


def quickstart_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
