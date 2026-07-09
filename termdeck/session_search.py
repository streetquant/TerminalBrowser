from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .models import SessionRecord
from .redaction import redact_text


CaptureResolver = Callable[[SessionRecord, int], str]


@dataclass(frozen=True)
class OutputSnippet:
    """A bounded line preview from a matched terminal capture."""

    line_number: int
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"line_number": self.line_number, "text": self.text}


@dataclass(frozen=True)
class SessionSearchMatch:
    """One browser-like search result across remembered terminal tabs."""

    session_id: str
    name: str
    tag: str
    window_title: str
    status: str
    command: str
    cwd: str
    matched_fields: tuple[str, ...] = field(default_factory=tuple)
    output_snippets: tuple[OutputSnippet, ...] = field(default_factory=tuple)
    redacted: bool = False
    redaction_labels: tuple[str, ...] = field(default_factory=tuple)
    score: int = 0

    @property
    def output_match_count(self) -> int:
        return len(self.output_snippets)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.session_id,
            "name": self.name,
            "tag": self.tag,
            "window_title": self.window_title,
            "status": self.status,
            "command": self.command,
            "cwd": self.cwd,
            "matched_fields": list(self.matched_fields),
            "output_snippets": [snippet.as_dict() for snippet in self.output_snippets],
            "output_match_count": self.output_match_count,
            "redacted": self.redacted,
            "redaction_labels": list(self.redaction_labels),
            "score": self.score,
        }


@dataclass(frozen=True)
class SessionSearchResult:
    query: str
    include_output: bool
    redacted: bool
    count: int
    matches: tuple[SessionSearchMatch, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "termdeck.session-search",
            "query": self.query,
            "include_output": self.include_output,
            "redacted": self.redacted,
            "count": self.count,
            "matches": [match.as_dict() for match in self.matches],
        }


_FIELD_ORDER = ("id", "name", "tag", "window", "status", "command", "directory")


def _terms(query: str) -> list[str]:
    return [term for term in str(query or "").casefold().split() if term]


def _record_fields(record: SessionRecord) -> dict[str, str]:
    return {
        "id": record.id,
        "name": record.name,
        "tag": record.tag,
        "window": record.window_title,
        "status": record.status,
        "command": record.command,
        "directory": record.cwd,
    }


def _contains_all_terms(text: str, terms: Sequence[str]) -> bool:
    haystack = str(text or "").casefold()
    return all(term in haystack for term in terms)


def _matched_metadata_fields(record: SessionRecord, terms: Sequence[str]) -> tuple[str, ...]:
    fields = _record_fields(record)
    joined = " ".join(fields.values())
    if not _contains_all_terms(joined, terms):
        return ()
    matched = []
    for name in _FIELD_ORDER:
        value = fields[name].casefold()
        if any(term in value for term in terms):
            matched.append(name)
    return tuple(matched)


def _metadata_score(record: SessionRecord, query: str, matched_fields: Sequence[str]) -> int:
    if not matched_fields:
        return 0
    query_cf = query.casefold().strip()
    score = 20 + len(matched_fields) * 3
    if record.name.casefold().startswith(query_cf):
        score += 40
    if record.tag.casefold().startswith(query_cf):
        score += 25
    if record.id.casefold().startswith(query_cf):
        score += 20
    if "name" in matched_fields:
        score += 15
    if "tag" in matched_fields:
        score += 10
    return score


def _capture_snippets(capture: str, terms: Sequence[str], *, max_snippets: int) -> tuple[OutputSnippet, ...]:
    snippets: list[OutputSnippet] = []
    for line_number, line in enumerate(str(capture or "").splitlines(), start=1):
        folded = line.casefold()
        if any(term in folded for term in terms):
            bounded = line if len(line) <= 240 else line[:237] + "..."
            snippets.append(OutputSnippet(line_number=line_number, text=bounded))
            if len(snippets) >= max(1, int(max_snippets)):
                break
    return tuple(snippets)


def search_sessions(
    records: Iterable[SessionRecord],
    query: str,
    *,
    include_output: bool = False,
    capture_resolver: CaptureResolver | None = None,
    lines: int = 120,
    redact: bool = True,
    max_results: int = 100,
    max_snippets: int = 3,
) -> SessionSearchResult:
    """Search remembered browser tabs by metadata and optional terminal output.

    Output search is opt-in because it may call tmux capture for many sessions.
    Captures are redacted before matching and serialisation unless ``redact`` is
    false. This keeps the default behaviour safe for support bundles and demos.
    """

    clean_query = str(query or "").strip()
    terms = _terms(clean_query)
    if not terms:
        return SessionSearchResult(query=clean_query, include_output=include_output, redacted=redact, count=0, matches=())

    matches: list[SessionSearchMatch] = []
    for order, record in enumerate(records):
        matched_fields = _matched_metadata_fields(record, terms)
        snippets: tuple[OutputSnippet, ...] = ()
        redaction_labels: tuple[str, ...] = ()
        was_redacted = False
        output_score = 0

        if include_output and capture_resolver is not None:
            raw_capture = capture_resolver(record, max(1, int(lines)))
            if redact:
                redacted_capture = redact_text(raw_capture)
                capture_text = redacted_capture.text
                was_redacted = bool(redacted_capture.replacements)
                redaction_labels = tuple(redacted_capture.labels)
            else:
                capture_text = raw_capture
            if _contains_all_terms(capture_text, terms):
                snippets = _capture_snippets(capture_text, terms, max_snippets=max_snippets)
                output_score = 12 + len(snippets) * 8

        if not matched_fields and not snippets:
            continue

        score = _metadata_score(record, clean_query, matched_fields) + output_score + max(0, 20 - order)
        matches.append(SessionSearchMatch(
            session_id=record.id,
            name=record.name,
            tag=record.tag,
            window_title=record.window_title,
            status=record.status,
            command=record.command,
            cwd=record.cwd,
            matched_fields=matched_fields,
            output_snippets=snippets,
            redacted=was_redacted,
            redaction_labels=redaction_labels,
            score=score,
        ))

    matches.sort(key=lambda item: (-item.score, item.window_title.casefold(), item.name.casefold(), item.session_id))
    limited = tuple(matches[: max(1, int(max_results))])
    return SessionSearchResult(
        query=clean_query,
        include_output=include_output,
        redacted=redact,
        count=len(limited),
        matches=limited,
    )


def render_search_table(result: SessionSearchResult) -> str:
    if not result.matches:
        return f"No TermDeck sessions matched: {result.query!r}"
    rows = [["ID", "Window", "Name", "Tag", "Status", "Fields", "Output", "Command"]]
    for match in result.matches:
        rows.append([
            match.session_id,
            match.window_title,
            match.name,
            match.tag,
            match.status,
            ",".join(match.matched_fields) or "-",
            str(match.output_match_count) if match.output_match_count else "-",
            match.command,
        ])
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[index]) for index, cell in enumerate(rows[0]))]
    lines.append("  ".join("-" * width for width in widths))
    for row, match in zip(rows[1:], result.matches):
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
        for snippet in match.output_snippets:
            lines.append(f"  ↳ line {snippet.line_number}: {snippet.text}")
        if match.redacted:
            labels = ", ".join(match.redaction_labels)
            lines.append(f"  ↳ redacted secret-like values: {labels}")
    return "\n".join(lines)
