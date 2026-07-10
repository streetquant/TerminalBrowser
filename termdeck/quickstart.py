from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .command_render import default_shell, directory_tab_name, normalise_directory
from .desktop_integration import build_install_plan
from .terminator_config import TerminatorConfig

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
            "required_for": "persistent terminal tabs",
        },
        "gdk3": {
            "ok": gdk_ok,
            "detail": "ok" if gdk_ok else "missing",
            "required_for": "GTK3 windowing",
        },
        "gtk3": {
            "ok": gtk_ok,
            "detail": "ok" if gtk_ok else "missing",
            "required_for": "native named windows and tabs",
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


def _directory_summary(path: str | Path | None) -> dict[str, object]:
    directory = normalise_directory(path)
    return {
        "path": directory,
        "default_tab_name": directory_tab_name(directory),
        "default_command": default_shell(),
    }


def _action(action_id: str, label: str, command: str, why: str) -> dict[str, str]:
    return {"id": action_id, "label": label, "command": command, "why": why}


def _recommended_actions(payload: dict[str, Any], *, path: Path) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    dependencies = payload["dependencies"]
    if not all(item["ok"] for item in dependencies.values()):
        actions.append(_action(
            "install-dependencies",
            "Install lightweight runtime dependencies",
            INSTALL_HINT,
            "TermDeck needs tmux for persistence and GTK/VTE for the native interface.",
        ))

    actions.append(_action(
        "open-directory-tab",
        "Open this directory as a tab",
        "python3 -m termdeck new " + shlex.quote(str(path)),
        f"Creates a shell tab named {directory_tab_name(path)!r}.",
    ))

    terminator = payload["terminator"]
    if terminator["found"]:
        actions.append(_action(
            "inspect-terminator-theme",
            "Inspect imported Terminator appearance and shortcuts",
            "python3 -m termdeck parse-terminator",
            "Confirms the font, colours, scrollback, and keybindings TermDeck will reuse.",
        ))
        if terminator["layouts"]:
            actions.append(_action(
                "convert-terminator-layout",
                "Import an existing Terminator layout",
                "python3 -m termdeck terminator-workspace --output terminator.workspace.json",
                "Migration utility for existing multi-window terminal layouts.",
            ))

    desktop = payload["desktop"]
    if not desktop["installed"]:
        actions.append(_action(
            "install-app-launcher",
            "Install desktop app launcher",
            "python3 -m termdeck install-desktop",
            "Lets users start TermDeck from the desktop or app menu.",
        ))

    actions.append(_action(
        "launch-gui",
        "Launch TermDeck",
        "python3 -m termdeck gui",
        "Opens the named-window and directory-tab interface.",
    ))
    return actions


def build_quickstart(
    path: str | Path | None = None,
    *,
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
        "directory": _directory_summary(root),
    }
    payload["ready_for_gui"] = bool(
        payload["dependencies"]["tmux"]["ok"]
        and payload["dependencies"]["gtk3"]["ok"]
        and payload["dependencies"]["vte"]["ok"]
    )
    payload["recommended_actions"] = _recommended_actions(payload, path=root)
    return payload


def quickstart_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
