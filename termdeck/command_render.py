from __future__ import annotations

import os
import shlex
from pathlib import Path


def default_shell() -> str:
    return os.environ.get("SHELL", "/bin/sh")


def render_command(command: str, *, shell: str | None = None, cwd: str | Path | None = None) -> str:
    """Render portable command placeholders used by saved workspace recipes."""

    rendered = command.replace("{shell}", shell or default_shell())
    if cwd is not None:
        rendered = rendered.replace("{cwd}", shlex.quote(str(Path(cwd).expanduser())))
    return rendered


def normalise_directory(path: str | Path | None) -> str:
    directory = Path(path or os.getcwd()).expanduser()
    try:
        directory = directory.resolve()
    except OSError:
        directory = directory.absolute()
    return str(directory)


def directory_tab_name(path: str | Path | None) -> str:
    """Return a compact default tab title from a working directory."""

    directory = Path(normalise_directory(path))
    if directory.name:
        return directory.name
    anchor = directory.anchor.rstrip("/\\")
    return anchor or "root"
