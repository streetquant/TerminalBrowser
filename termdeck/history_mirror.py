"""A bounded, provenance-aware local mirror of terminal scrollback.

The mirror is deliberately independent of terminal UI and tmux integration.  It
allows callers to permit native scrolling only where each visible backend line
has an accountable local copy.  Callers normally serialize their own control
mode events; a lock still protects mutations so an accidental cross-thread call
cannot corrupt absolute-index bookkeeping.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from threading import Lock


class Provenance(Enum):
    """How a line entered the mirror, so diagnostics retain its origin."""

    LIVE_OUTPUT = auto()
    BACKFILL = auto()
    PREFETCH = auto()


@dataclass(frozen=True)
class MirrorLine:
    """One faithfully copied backend line and the operation that supplied it."""

    text: str
    provenance: Provenance


@dataclass(frozen=True)
class PrefetchRequest:
    """A bounded request for lines immediately preceding the held range."""

    start_line: int
    line_count: int


@dataclass(frozen=True)
class MirrorRange:
    """The absolute interval that the local mirror can account for honestly."""

    start_line: int
    end_line: int
    contiguous: bool


class HistoryMirror:
    """Maintain a bounded local history interval without overstating fidelity.

    ``start_line`` is never derived from a deque position alone: it is an
    absolute backend coordinate, advanced when old content is evicted and
    lowered only by an abutting prefetch.  A capacity eviction is not a missing
    section in the remaining interval, whereas an observed transport gap or a
    non-abutting prefetch invalidates the contiguous claim until a fresh seed.
    """

    def __init__(
        self,
        *,
        max_lines: int = 10_000,
        page_size: int = 2_000,
        prefetch_margin: int = 400,
    ) -> None:
        """Create an empty mirror with a positive bounded capacity.

        Positive sizes avoid ambiguous zero-capacity ranges and ensure a line
        finalized from live output always has a representable slot.
        """
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if prefetch_margin < 0:
            raise ValueError("prefetch_margin must not be negative")

        self._max_lines = max_lines
        self._page_size = page_size
        self._prefetch_margin = prefetch_margin
        self._lines: deque[MirrorLine] = deque()
        # The empty interval is meaningful after an empty backfill, so keep an
        # explicit start rather than infer it only from the deque.
        self._start_line = 0
        self._next_live_line = 0
        self._known_backend_extent = 0
        self._pending_partial = ""
        self._contiguous = True
        self._pending_prefetches: set[PrefetchRequest] = set()
        self._evicted = 0
        self._gaps = 0
        self._lock = Lock()

    def seed_backfill(
        self,
        lines: Sequence[str],
        *,
        start_line: int,
        total_history_lines: int,
    ) -> None:
        """Replace the mirror with an authoritative capture and restore trust.

        A seed is the recovery boundary after a gap.  If the supplied capture
        exceeds capacity, retaining its newest suffix preserves the live edge;
        that suffix remains a contiguous, faithfully mirrored interval.
        """
        self._require_non_negative(start_line, "start_line")
        self._require_non_negative(total_history_lines, "total_history_lines")
        materialized = list(lines)
        supplied_end = start_line + len(materialized)
        if total_history_lines < supplied_end:
            raise ValueError("total_history_lines cannot precede the supplied backfill")

        with self._lock:
            discarded = max(0, len(materialized) - self._max_lines)
            kept = materialized[discarded:]
            self._lines = deque(
                MirrorLine(text, Provenance.BACKFILL) for text in kept
            )
            self._start_line = start_line + discarded
            self._next_live_line = supplied_end
            self._known_backend_extent = total_history_lines
            self._pending_partial = ""
            self._contiguous = True
            self._pending_prefetches.clear()
            self._evicted += discarded

    def feed_output(self, text: str) -> int:
        """Consume terminal output and return the exclusive live-line end index.

        Only newline-terminated text becomes a backend line.  A lone carriage
        return is a terminal progress update, not another line: it discards the
        pending partial text so the following update replaces it.  CRLF remains
        a conventional completed line ending.
        """
        with self._lock:
            position = 0
            while position < len(text):
                character = text[position]
                if character == "\n":
                    self._append_line(MirrorLine(self._pending_partial, Provenance.LIVE_OUTPUT))
                    self._pending_partial = ""
                    position += 1
                elif character == "\r":
                    if position + 1 < len(text) and text[position + 1] == "\n":
                        self._append_line(
                            MirrorLine(self._pending_partial, Provenance.LIVE_OUTPUT)
                        )
                        self._pending_partial = ""
                        position += 2
                    else:
                        # A progress display redraws the current unterminated
                        # line; retaining it would concatenate stale progress.
                        self._pending_partial = ""
                        position += 1
                else:
                    self._pending_partial += character
                    position += 1
            return self._next_live_line

    def note_gap(self) -> None:
        """Record externally detected loss; only a fresh seed can restore trust."""
        with self._lock:
            self._mark_gap()

    def mirrored_range(self) -> MirrorRange:
        """Return the exact held interval and whether it has no known hole."""
        with self._lock:
            return MirrorRange(
                start_line=self._start_line,
                end_line=self._end_line(),
                contiguous=self._contiguous,
            )

    def line_at(self, absolute_line: int) -> MirrorLine | None:
        """Return one held line, or ``None`` when its absolute index is absent."""
        with self._lock:
            offset = absolute_line - self._start_line
            if offset < 0 or offset >= len(self._lines):
                return None
            return self._lines[offset]

    def lines(self, start_line: int, count: int) -> list[MirrorLine]:
        """Return the requested interval clipped to the content still held."""
        if count <= 0:
            return []
        with self._lock:
            request_end = start_line + count
            first = max(start_line, self._start_line)
            last = min(request_end, self._end_line())
            if last <= first:
                return []
            start_offset = first - self._start_line
            end_offset = last - self._start_line
            # deque slicing is unavailable; materialize only the bounded held
            # range, which is already limited by max_lines.
            return list(self._lines)[start_offset:end_offset]

    def needs_prefetch(self, viewport_top_line: int) -> PrefetchRequest | None:
        """Request the page immediately before the range when its top is near.

        Pending requests are remembered because callers may ask this policy on
        every viewport update while a backend fetch is still in flight.
        """
        with self._lock:
            if self._start_line == 0:
                return None
            if viewport_top_line > self._start_line + self._prefetch_margin:
                return None
            request_start = max(0, self._start_line - self._page_size)
            request = PrefetchRequest(
                start_line=request_start,
                line_count=self._start_line - request_start,
            )
            if request in self._pending_prefetches:
                return None
            self._pending_prefetches.add(request)
            return request

    def apply_prefetch(self, lines: Sequence[str], *, start_line: int) -> bool:
        """Stitch an immediately preceding chunk, or flag an unsafe discontinuity.

        Reapplying a chunk that is already fully represented is harmless.  This
        makes late duplicate backend replies safe, while any other non-abutting
        reply is deliberately treated as a gap rather than silently relabeled.
        """
        self._require_non_negative(start_line, "start_line")
        materialized = list(lines)
        incoming_end = start_line + len(materialized)

        with self._lock:
            self._clear_pending_for_start(start_line)
            current_end = self._end_line()

            if self._range_contains(start_line, incoming_end):
                return True

            if not self._lines:
                # An empty mirror has no held interval to contradict.  This is
                # useful before a first seed, while an empty seeded range still
                # follows the regular abutment rule below.
                if self._start_line == 0 and self._next_live_line == 0:
                    self._install_initial_prefetch(materialized, start_line)
                    return True

            if incoming_end != self._start_line:
                self._mark_gap()
                return False

            # Preserve the most recent tail when capacity is exhausted.  The
            # oldest requested lines are the ones a ring would immediately
            # evict, so this cannot introduce a hole in the retained interval.
            available = self._max_lines - len(self._lines)
            if available > 0:
                accepted = materialized[-available:]
                if accepted:
                    self._lines.extendleft(
                        MirrorLine(text, Provenance.PREFETCH)
                        for text in reversed(accepted)
                    )
                    self._start_line -= len(accepted)
            discarded = len(materialized) - min(len(materialized), max(available, 0))
            self._evicted += discarded
            return True

    def known_backend_extent(self) -> int:
        """Return the latest total backend extent known to this mirror."""
        with self._lock:
            return self._known_backend_extent

    def checksum_audit(self, absolute_line: int, expected_text: str) -> bool:
        """Spot-check one held line and invalidate continuity on disagreement."""
        with self._lock:
            offset = absolute_line - self._start_line
            line = (
                self._lines[offset]
                if 0 <= offset < len(self._lines)
                else None
            )
            if line is not None and line.text == expected_text:
                return True
            self._mark_gap()
            return False

    def stats(self) -> dict[str, int | bool]:
        """Expose concise state for diagnostics without leaking mutable content."""
        with self._lock:
            return {
                "lines_held": len(self._lines),
                "evicted": self._evicted,
                "gaps": self._gaps,
                "pending_prefetches": len(self._pending_prefetches),
                "contiguous": self._contiguous,
            }

    def _append_line(self, line: MirrorLine) -> None:
        """Append at the immutable live cursor, evicting only the old prefix."""
        expected_end = self._end_line()
        if self._next_live_line != expected_end:
            # This should be unreachable with the retention policy, but never
            # claim a continuous interval if a future caller violates it.
            self._mark_gap()
            self._next_live_line = expected_end
        self._lines.append(line)
        self._next_live_line += 1
        self._known_backend_extent = max(self._known_backend_extent, self._next_live_line)
        if len(self._lines) > self._max_lines:
            self._lines.popleft()
            self._start_line += 1
            self._evicted += 1

    def _end_line(self) -> int:
        """Return the exclusive endpoint derived from absolute start and length."""
        return self._start_line + len(self._lines)

    def _range_contains(self, start_line: int, end_line: int) -> bool:
        """Whether an incoming half-open interval is fully already held."""
        return self._start_line <= start_line and end_line <= self._end_line()

    def _install_initial_prefetch(self, lines: Sequence[str], start_line: int) -> None:
        """Initialize an otherwise unpositioned mirror from a first prefetch."""
        discarded = max(0, len(lines) - self._max_lines)
        kept = lines[discarded:]
        self._lines = deque(MirrorLine(text, Provenance.PREFETCH) for text in kept)
        self._start_line = start_line + discarded
        self._next_live_line = start_line + len(lines)
        self._known_backend_extent = max(self._known_backend_extent, self._next_live_line)
        self._evicted += discarded

    def _clear_pending_for_start(self, start_line: int) -> None:
        """Finish any request whose response begins at this backend coordinate."""
        self._pending_prefetches = {
            request
            for request in self._pending_prefetches
            if request.start_line != start_line
        }

    def _mark_gap(self) -> None:
        """Count every detected fidelity failure for postmortem diagnostics."""
        self._contiguous = False
        self._gaps += 1

    @staticmethod
    def _require_non_negative(value: int, name: str) -> None:
        """Reject impossible absolute coordinates at the public boundary."""
        if value < 0:
            raise ValueError(f"{name} must not be negative")
