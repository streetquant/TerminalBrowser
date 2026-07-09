from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_ID = "io.github.termdeck.TermDeck"
DESKTOP_FILENAME = f"{APP_ID}.desktop"
ICON_NAME = "termdeck"

TERMDECK_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#1e1e1e"/>
  <rect x="14" y="18" width="100" height="92" rx="10" fill="#101010" stroke="#5f6368" stroke-width="4"/>
  <rect x="14" y="18" width="100" height="20" rx="10" fill="#2b2b2b"/>
  <circle cx="29" cy="28" r="4" fill="#ef4444"/>
  <circle cx="43" cy="28" r="4" fill="#f59e0b"/>
  <circle cx="57" cy="28" r="4" fill="#22c55e"/>
  <path d="M31 58l16 14-16 14" fill="none" stroke="#8ae234" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M58 88h34" fill="none" stroke="#d3d7cf" stroke-width="8" stroke-linecap="round"/>
</svg>
"""


@dataclass(frozen=True)
class DesktopPaths:
    data_home: Path
    applications_dir: Path
    icon_dir: Path
    desktop_file: Path
    icon_file: Path


@dataclass(frozen=True)
class DesktopInstallPlan:
    command: str
    paths: DesktopPaths
    desktop_entry: str
    icon_svg: str = TERMDECK_ICON_SVG

    def as_dict(self) -> dict[str, object]:
        return {
            "app_id": APP_ID,
            "command": self.command,
            "desktop_file": str(self.paths.desktop_file),
            "icon_file": str(self.paths.icon_file),
            "applications_dir": str(self.paths.applications_dir),
            "icon_dir": str(self.paths.icon_dir),
            "desktop_entry": self.desktop_entry,
        }


def xdg_data_home(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    raw = env.get("XDG_DATA_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def desktop_paths(data_home: str | Path | None = None) -> DesktopPaths:
    root = Path(data_home).expanduser() if data_home is not None else xdg_data_home()
    applications_dir = root / "applications"
    icon_dir = root / "icons" / "hicolor" / "scalable" / "apps"
    return DesktopPaths(
        data_home=root,
        applications_dir=applications_dir,
        icon_dir=icon_dir,
        desktop_file=applications_dir / DESKTOP_FILENAME,
        icon_file=icon_dir / f"{ICON_NAME}.svg",
    )


def resolve_launch_command(command: str | None = None) -> str:
    """Return a desktop Exec command that does not require users to know Python.

    Prefer an installed `termdeck` entry point. When running straight from a
    source tree, fall back to the current interpreter with `-m termdeck`.
    """
    if command and command.strip():
        base = command.strip()
    else:
        found = shutil.which("termdeck")
        base = shlex.quote(found) if found else shlex.join([sys.executable, "-m", "termdeck"])
    if " gui" in base or base.endswith(" gui"):
        return base
    return f"{base} gui"


def build_desktop_entry(exec_command: str) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=TermDeck",
        "GenericName=Terminal Task Manager",
        "Comment=Browser-like task manager for terminal processes",
        f"Exec={exec_command}",
        f"Icon={ICON_NAME}",
        "Terminal=false",
        "StartupNotify=true",
        "Categories=System;TerminalEmulator;Development;Utility;",
        "Keywords=terminal;tmux;codex;shell;process;task manager;browser tabs;",
    ]
    return "\n".join(lines) + "\n"


def build_install_plan(*, command: str | None = None, data_home: str | Path | None = None) -> DesktopInstallPlan:
    exec_command = resolve_launch_command(command)
    return DesktopInstallPlan(
        command=exec_command,
        paths=desktop_paths(data_home),
        desktop_entry=build_desktop_entry(exec_command),
    )


def install_desktop(plan: DesktopInstallPlan, *, dry_run: bool = False, update_database: bool = False) -> dict[str, object]:
    payload = plan.as_dict()
    payload.update({"action": "install-desktop", "dry_run": dry_run, "ok": True, "written": []})
    if dry_run:
        return payload

    plan.paths.applications_dir.mkdir(parents=True, exist_ok=True)
    plan.paths.icon_dir.mkdir(parents=True, exist_ok=True)
    plan.paths.desktop_file.write_text(plan.desktop_entry, encoding="utf-8")
    plan.paths.icon_file.write_text(plan.icon_svg, encoding="utf-8")
    plan.paths.desktop_file.chmod(0o755)
    payload["written"] = [str(plan.paths.desktop_file), str(plan.paths.icon_file)]

    if update_database:
        updater = shutil.which("update-desktop-database")
        if updater:
            result = subprocess.run([updater, str(plan.paths.applications_dir)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            payload["update_desktop_database"] = {"returncode": result.returncode, "stderr": result.stderr.strip()}
    return payload


def uninstall_desktop(*, data_home: str | Path | None = None, dry_run: bool = False) -> dict[str, object]:
    paths = desktop_paths(data_home)
    targets = [paths.desktop_file, paths.icon_file]
    payload = {
        "action": "uninstall-desktop",
        "dry_run": dry_run,
        "ok": True,
        "targets": [str(target) for target in targets],
        "removed": [],
    }
    if dry_run:
        return payload
    removed: list[str] = []
    for target in targets:
        try:
            target.unlink()
            removed.append(str(target))
        except FileNotFoundError:
            continue
    payload["removed"] = removed
    return payload
