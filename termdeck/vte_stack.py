from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path


_TERM_NAME = "termdeck-vte"
_BASE_TERM = "xterm-256color"
_FALLBACK_TERM = "teraterm-256color"
_TERMINFO_LOCK = threading.Lock()
_TERMINFO_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_ATTACH_ENVIRONMENT: dict[str, str] | None = None


def _without_alternate_screen(source: str) -> str:
    """Return an xterm-compatible entry that never switches VTE buffers."""

    lines = source.splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line and not line.startswith("#")),
        None,
    )
    if header_index is None:
        raise RuntimeError("infocmp returned no terminal entry")
    lines[header_index] = f"{_TERM_NAME}|TermDeck VTE client without alternate screen,"
    lines = [
        line
        for line in lines
        if not re.match(r"^\s*(?:smcup|rmcup)(?:=|@)", line)
    ]
    return "\n".join(lines) + "\n"


def _compiled_environment() -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
    source = subprocess.run(
        ["infocmp", "-1", "-x", _BASE_TERM],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    transformed = _without_alternate_screen(source)
    directory = tempfile.TemporaryDirectory(prefix="termdeck-vte-terminfo-")
    root = Path(directory.name)
    try:
        subprocess.run(
            ["tic", "-x", "-o", str(root), "-"],
            input=transformed,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        compiled = subprocess.run(
            ["infocmp", "-A", str(root), "-1", "-x", _TERM_NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        if re.search(r"^\s*(?:smcup|rmcup)(?:=|@)", compiled, re.MULTILINE):
            raise RuntimeError("compiled TermDeck entry still changes screen buffers")
        if not re.search(r"^\s*colors#(?:0x100|256),", compiled, re.MULTILINE):
            raise RuntimeError("compiled TermDeck entry is not 256-colour capable")
        if not re.search(r"^\s*(?:clear|cup)=", compiled, re.MULTILINE):
            raise RuntimeError("compiled TermDeck entry lacks cursor-addressing support")
    except Exception:
        directory.cleanup()
        raise
    return (
        {
            "TERM": _TERM_NAME,
            "TERMINFO": str(root),
            "COLORTERM": "truecolor",
            "TERM_PROGRAM": "TermDeck",
        },
        directory,
    )


def _stock_fallback_environment() -> dict[str, str]:
    """Use the stock no-alternate-screen entry without a second subprocess.

    The primary path already performs a strict ``infocmp``/``tic``/``infocmp``
    round trip.  When that bounded tool chain is unavailable or starved, doing
    another synchronous ``infocmp`` in the GTK construction path can make every
    terminal fail to open under host load.  ``teraterm-256color`` is the
    deliberately selected system fallback; release and VTE-stack tests inspect
    its capabilities independently, while runtime fallback remains immediate.
    """

    return {
        "TERM": _FALLBACK_TERM,
        "COLORTERM": "truecolor",
        "TERM_PROGRAM": "TermDeck",
    }


#: ``light foreground;dark background`` in the COLORFGBG convention.
DARK_COLORFGBG = "15;0"


def vte_attach_environment() -> dict[str, str]:
    """Environment for one tmux client rendered into TermDeck's VTE.

    tmux normally switches its outer client into the alternate screen. VTE has
    no scrollback there, so a wheel event can only ask tmux to redraw history;
    that redraw invalidates VTE's coordinate-based text selection. Terminator's
    selection behaviour depends on one normal-screen VTE buffer instead. This
    process-local terminfo entry keeps all tmux rendering in that buffer while
    preserving the xterm-256color capability set and true-colour hint.
    """

    global _ATTACH_ENVIRONMENT, _TERMINFO_DIRECTORY
    with _TERMINFO_LOCK:
        if _ATTACH_ENVIRONMENT is None:
            try:
                environment, directory = _compiled_environment()
            except (OSError, subprocess.SubprocessError, RuntimeError):
                environment = _stock_fallback_environment()
                directory = None
            _ATTACH_ENVIRONMENT = environment
            _TERMINFO_DIRECTORY = directory
        return dict(_ATTACH_ENVIRONMENT)


def merge_vte_attach_environment(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment.update(vte_attach_environment())
    # Every shipped terminal palette is dark. Codex and other TUIs read
    # COLORFGBG to pick their ink; an inherited light value is what makes
    # them paint near-black headings, so state the truth for the child.
    environment["COLORFGBG"] = DARK_COLORFGBG
    return environment
