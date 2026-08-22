from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


# Signature "t1000 ink" 16-color set. Tuned for the chrome-matched dark
# surface (#0a0d13): ANSI black is a visible slate instead of #000000 so
# full-screen TUIs that print black/bright-black text stay readable, and
# the accent hues mirror the app chrome (periwinkle / rose / mint / amber).
DEFAULT_PALETTE = (
    "#5a6488", "#f7768e", "#9ece6a", "#e0af68",
    "#82aaff", "#bb9af7", "#7dcfff", "#c8d3ea",
    "#737ea3", "#ff9db4", "#b9f27c", "#f2cd8a",
    "#a9c5ff", "#d5b8ff", "#b0e7ff", "#f1f5ff",
)


@dataclass(frozen=True)
class TerminalTheme:
    font: str = "Monospace 12"
    use_system_font: bool = False
    # Chrome-matched defaults: the built-in terminal surface blends with the
    # dark chrome (bg_sunken) instead of shipping a jarring pure-black pane.
    # Terminator imports and palette packs still override these values.
    foreground_color: str = "#dfe6f2"
    background_color: str = "#0a0d13"
    palette: tuple[str, ...] = DEFAULT_PALETTE
    cursor_color: str | None = None
    cursor_shape: str = "block"
    cursor_blink: bool | None = None
    scrollback_lines: int = 10000
    window_width: int | None = None
    window_height: int | None = None
    keybindings: Mapping[str, str] = field(default_factory=dict)
    source_path: Path | None = None
    profile_name: str = "default"


@dataclass(frozen=True)
class SessionRecord:
    id: str
    name: str
    tag: str
    command: str
    cwd: str
    tmux_name: str
    created_at: str
    last_opened_at: str
    status: str = "new"
    last_pid: int | None = None
    window_id: str = "main"
    window_title: str = "Main"
    tab_index: int = 0
    visible: bool = True
    pinned: bool = False
    pane_target: str | None = None
    external: bool = False
    recovery_command: str | None = None
    recovery_cwd: str | None = None
    auth_scope_id: str | None = None
    codex_home: str | None = None
    selected_account: str | None = None
    pending_account: str | None = None
    auth_restart_required: bool = False
    auth_changed_at: str | None = None
    # Generic, application-independent restart authority.  The JSON is
    # canonical and bound to ``recovery_recipe_sha256``; legacy agent/account
    # metadata remains optional compatibility data rather than a prerequisite
    # for session recovery.
    recovery_recipe: str | None = None
    recovery_recipe_sha256: str | None = None
    restart_policy: str = "on-failure"
    recovery_attempts: int = 0
    recovery_last_attempt_at: str | None = None
    recovery_last_error: str | None = None


@dataclass(frozen=True)
class ResourceSnapshot:
    pid: int | None
    exists: bool
    state: str = "unknown"
    cpu_percent: float = 0.0
    memory_bytes: int = 0
    runtime_seconds: float = 0.0
    process_count: int = 0

    @property
    def memory_mib(self) -> float:
        return self.memory_bytes / (1024 * 1024)

    @property
    def runtime_label(self) -> str:
        seconds = max(0, int(self.runtime_seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {secs:02d}s"
        return f"{secs}s"
