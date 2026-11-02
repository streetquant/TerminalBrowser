"""Bind account routing at each new terminal process, independently of tmux."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Mapping

ACCOUNT_SELECTORS = (
    "CODEX_HOME", "CODEX_AUTH_HOME", "OPENAI_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_SQLITE_HOME",
)
LAUNCH_VARIABLES = ("HOME", "USER", "LOGNAME", "SHELL", "PATH")


def bind_account_environment(command: str, environment: Mapping[str, str]) -> list[str]:
    """Apply caller/recipe selector values and remove absent inherited values.

    A long-lived tmux server has a global environment from its first caller.
    Its new-session -e NAME syntax does not mask a missing session value.
    Execute env locally instead; existing panes and server state stay intact.
    """
    arguments = ["/usr/bin/env"]
    for name in ACCOUNT_SELECTORS:
        if name not in environment:
            arguments.extend(["-u", name])
    arguments.extend(f"{name}={environment[name]}" for name in ACCOUNT_SELECTORS
                     if name in environment)
    # tmux's -e PATH does not reliably replace the client's process PATH,
    # particularly on respawn. Bind the resolved recipe at the exec boundary.
    arguments.extend(f"{name}={environment[name]}" for name in LAUNCH_VARIABLES
                     if name in environment)
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    # Keep a direct Bash terminal's process identity. Opaque shell syntax runs
    # unchanged inside the same supervisor shell used by private launches.
    direct_bash = bool(argv) and Path(argv[0]).name.casefold() == "bash" and "-c" not in argv and not any(
        any(character in word for character in "|&;<>()$" + chr(96)) for word in argv)
    if direct_bash:
        arguments.extend(argv)
    else:
        arguments.extend(["/bin/bash", "--noprofile", "--norc", "-c", command])
    return arguments
