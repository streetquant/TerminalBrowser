from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


DEFAULT_PALETTE = (
    "#000000", "#cc0000", "#4e9a06", "#c4a000",
    "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f",
    "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
)


@dataclass(frozen=True)
class TerminalTheme:
    font: str = "Monospace 12"
    use_system_font: bool = False
    foreground_color: str = "#d3d7cf"
    background_color: str = "#000000"
    palette: tuple[str, ...] = DEFAULT_PALETTE
    cursor_color: str | None = None
    cursor_shape: str = "block"
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
