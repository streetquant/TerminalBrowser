from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ScrollDisposition(Enum):
    """Describe who owns one terminal scroll gesture."""

    UNHANDLED = "unhandled"
    HANDLED = "handled"
    PASSTHROUGH = "passthrough"


class ChildInputProtocol(Enum):
    """Byte protocol negotiated by a child-owned alternate-screen TUI."""

    SGR_MOUSE = "sgr-mouse"
    LEGACY_MOUSE = "legacy-mouse"


class HistoryOwner(Enum):
    """Canonical owner of the history surface currently being reviewed."""

    LIVE = "live"
    TMUX = "tmux-history"
    CODEX = "codex-transcript"
    CHILD_TUI = "child-tui"


class EdgeKnowledge(Enum):
    """What TermDeck can prove about the post-scroll live/history boundary."""

    LIVE_EXACT = "live-exact"
    HISTORY_EXACT = "history-exact"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendHistoryState:
    """Backend-owned history state bound to one live tmux pane.

    ``depth`` is always measured away from the live edge. For tmux copy mode
    it is the exact ``scroll_position`` reported by tmux. For Codex it is a
    TermDeck-owned logical depth accumulated only from acknowledged transcript
    navigation commands. Mixing those two coordinate systems with VTE's
    emulator scrollback is deliberately impossible here.
    """

    owner: HistoryOwner
    depth: int = 0
    extent: int | None = None
    managed_open: bool = False
    command: str = ""
    validated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("history depth cannot be negative")
        if self.extent is not None and self.extent < 0:
            raise ValueError("history extent cannot be negative")
        if self.owner is HistoryOwner.LIVE and self.depth:
            raise ValueError("live history state cannot have non-zero depth")
        if self.owner is not HistoryOwner.CODEX and self.managed_open:
            raise ValueError("only a Codex transcript may be TermDeck-managed")


@dataclass(frozen=True)
class ScrollOutcome:
    """Explicit post-scroll evidence returned by the canonical history owner.

    The object intentionally rejects truth-value coercion. The regression
    which made one downward wheel tick jump to the bottom was enabled by a
    lossy ``if result:`` check that discarded owner and edge information.
    Callers must inspect ``disposition`` and ``edge`` explicitly.
    """

    disposition: ScrollDisposition
    owner: HistoryOwner
    edge: EdgeKnowledge
    requested_lines: int
    depth: int | None = None
    extent: int | None = None
    managed_open: bool = False
    child_input_protocol: ChildInputProtocol | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.child_input_protocol is not None and not (
            self.disposition is ScrollDisposition.PASSTHROUGH
            and self.owner is HistoryOwner.CHILD_TUI
        ):
            raise ValueError(
                "a child input protocol is valid only for child-TUI passthrough"
            )

    def __bool__(self) -> bool:  # pragma: no cover - exercised by guard test
        raise TypeError("ScrollOutcome must not be used as a boolean")

    @property
    def handled(self) -> bool:
        return self.disposition is ScrollDisposition.HANDLED

    @property
    def reached_live_edge(self) -> bool:
        return self.edge is EdgeKnowledge.LIVE_EXACT


def unhandled_scroll(
    lines: int,
    *,
    detail: str = "",
    edge: EdgeKnowledge = EdgeKnowledge.UNKNOWN,
    depth: int | None = None,
) -> ScrollOutcome:
    """Return a non-navigation result without inventing live-edge evidence."""

    clean_depth = None if depth is None else max(0, int(depth))
    return ScrollOutcome(
        disposition=ScrollDisposition.UNHANDLED,
        owner=HistoryOwner.LIVE,
        edge=edge,
        requested_lines=int(lines),
        depth=clean_depth,
        detail=detail,
    )


def passthrough_scroll(
    lines: int,
    *,
    detail: str = "",
    protocol: ChildInputProtocol | None = None,
) -> ScrollOutcome:
    return ScrollOutcome(
        disposition=ScrollDisposition.PASSTHROUGH,
        owner=HistoryOwner.CHILD_TUI,
        edge=EdgeKnowledge.UNKNOWN,
        requested_lines=int(lines),
        child_input_protocol=protocol,
        detail=detail,
    )
