from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .models import SessionRecord
from .redaction import redact_text


class CaptureBackend(Protocol):
    def capture(self, record: SessionRecord, lines: int = 200) -> str:  # pragma: no cover - protocol only
        ...


@dataclass(frozen=True)
class OutputMatch:
    """One match in captured terminal output.

    The matching step can inspect the raw capture, but display text should be
    redacted before returning results. This lets users find output across many
    tabs without accidentally copying tokens into the side pane or issue logs.
    """

    line_number: int
    line: str
    context_before: tuple[str, ...] = ()
    context_after: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "line_number": self.line_number,
            "line": self.line,
            "context_before": list(self.context_before),
            "context_after": list(self.context_after),
        }


@dataclass(frozen=True)
class SessionOutputSearchResult:
    id: str
    name: str
    tag: str
    window: str
    status: str
    command: str
    cwd: str
    match_count: int
    matches: tuple[OutputMatch, ...] = ()
    capture_lines: int = 0
    redacted: bool = False
    redaction_labels: tuple[str, ...] = ()
    error: str = ""

    @property
    def has_matches(self) -> bool:
        return self.match_count > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "window": self.window,
            "status": self.status,
            "command": self.command,
            "cwd": self.cwd,
            "match_count": self.match_count,
            "matches": [match.as_dict() for match in self.matches],
            "capture_lines": self.capture_lines,
            "redacted": self.redacted,
            "redaction_labels": list(self.redaction_labels),
            "error": self.error,
        }


@dataclass(frozen=True)
class OutputSearchSummary:
    query: str
    terms: tuple[str, ...]
    searched_sessions: int
    matched_sessions: int
    match_count: int
    results: tuple[SessionOutputSearchResult, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "search-output",
            "query": self.query,
            "terms": list(self.terms),
            "searched_sessions": self.searched_sessions,
            "matched_sessions": self.matched_sessions,
            "match_count": self.match_count,
            "sessions": [result.as_dict() for result in self.results],
        }


def normalise_search_terms(query: str) -> tuple[str, ...]:
    """Split a simple browser-like query into literal terms.

    TermDeck intentionally does not expose regex syntax here. Users need a safe
    cross-tab finder, not a miniature grep language in the first-run path.
    """

    return tuple(term for term in query.casefold().split() if term)


def _line_matches(line: str, terms: tuple[str, ...], *, case_sensitive: bool = False) -> bool:
    haystack = line if case_sensitive else line.casefold()
    needles = terms if not case_sensitive else tuple(term for term in terms)
    return bool(needles) and all(term in haystack for term in needles)


def _redact_lines(lines: list[str]) -> tuple[list[str], bool, tuple[str, ...]]:
    text = "\n".join(lines)
    redacted = redact_text(text)
    return redacted.text.splitlines(), bool(redacted.replacements), tuple(redacted.labels)


def search_capture_text(
    text: str,
    query: str,
    *,
    context: int = 1,
    limit: int = 20,
    redact: bool = True,
    case_sensitive: bool = False,
) -> tuple[tuple[OutputMatch, ...], int, bool, tuple[str, ...]]:
    """Search one terminal capture and return redacted display matches.

    The returned integer is the total raw match count before `limit` truncation.
    """

    clean_context = max(0, int(context))
    clean_limit = max(1, int(limit))
    raw_lines = text.splitlines()
    terms = normalise_search_terms(query) if not case_sensitive else tuple(term for term in query.split() if term)
    indexes = [idx for idx, line in enumerate(raw_lines) if _line_matches(line, terms, case_sensitive=case_sensitive)]
    if not indexes:
        return (), 0, False, ()

    matches: list[OutputMatch] = []
    redacted_any = False
    labels: set[str] = set()
    for idx in indexes[:clean_limit]:
        start = max(0, idx - clean_context)
        end = min(len(raw_lines), idx + clean_context + 1)
        display_lines = raw_lines[start:end]
        if redact:
            display_lines, did_redact, redaction_labels = _redact_lines(display_lines)
            redacted_any = redacted_any or did_redact
            labels.update(redaction_labels)
        local_idx = idx - start
        line = display_lines[local_idx] if 0 <= local_idx < len(display_lines) else ""
        before = tuple(display_lines[:local_idx])
        after = tuple(display_lines[local_idx + 1 :])
        matches.append(OutputMatch(line_number=idx + 1, line=line, context_before=before, context_after=after))
    return tuple(matches), len(indexes), redacted_any, tuple(sorted(labels))


def search_session_output(
    records: Iterable[SessionRecord],
    query: str,
    *,
    backend: CaptureBackend,
    lines: int = 500,
    context: int = 1,
    per_session_limit: int = 20,
    redact: bool = True,
    include_empty: bool = False,
    case_sensitive: bool = False,
) -> OutputSearchSummary:
    """Search captured output across remembered terminal sessions."""

    records_list = list(records)
    terms = normalise_search_terms(query) if not case_sensitive else tuple(term for term in query.split() if term)
    if not terms:
        return OutputSearchSummary(query=query, terms=terms, searched_sessions=len(records_list), matched_sessions=0, match_count=0, results=())

    results: list[SessionOutputSearchResult] = []
    total_matches = 0
    for record in records_list:
        error = ""
        text = ""
        try:
            text = backend.capture(record, lines=max(1, int(lines)))
        except Exception as exc:  # pragma: no cover - exercised through CLI mocks
            error = str(exc)
        matches, match_count, redacted, labels = search_capture_text(
            text,
            query,
            context=context,
            limit=per_session_limit,
            redact=redact,
            case_sensitive=case_sensitive,
        ) if text else ((), 0, False, ())
        total_matches += match_count
        if match_count or include_empty or error:
            results.append(
                SessionOutputSearchResult(
                    id=record.id,
                    name=record.name,
                    tag=record.tag,
                    window=record.window_title,
                    status=record.status,
                    command=record.command,
                    cwd=record.cwd,
                    match_count=match_count,
                    matches=matches,
                    capture_lines=len(text.splitlines()) if text else 0,
                    redacted=redacted,
                    redaction_labels=labels,
                    error=error,
                )
            )
    return OutputSearchSummary(
        query=query,
        terms=terms,
        searched_sessions=len(records_list),
        matched_sessions=sum(1 for result in results if result.match_count > 0),
        match_count=total_matches,
        results=tuple(results),
    )


def render_output_search_table(summary: OutputSearchSummary) -> str:
    if not summary.terms:
        return "No query terms supplied."
    if not summary.results:
        return f"No matches for: {summary.query}"
    lines = [
        f"Search output: {summary.query}",
        f"Matched {summary.match_count} lines in {summary.matched_sessions}/{summary.searched_sessions} sessions.",
    ]
    for result in summary.results:
        if result.error:
            lines.append(f"\n{result.id}  [{result.tag}] {result.name}  error: {result.error}")
            continue
        if result.match_count == 0:
            lines.append(f"\n{result.id}  [{result.tag}] {result.name}  no matches")
            continue
        suffix = " redacted" if result.redacted else ""
        lines.append(f"\n{result.id}  [{result.tag}] {result.name}  {result.match_count} match(es){suffix}")
        for match in result.matches:
            for before in match.context_before:
                lines.append(f"  {match.line_number - len(match.context_before):>5}- {before}")
            lines.append(f"  {match.line_number:>5}: {match.line}")
            for offset, after in enumerate(match.context_after, start=1):
                lines.append(f"  {match.line_number + offset:>5}- {after}")
    return "\n".join(lines)
