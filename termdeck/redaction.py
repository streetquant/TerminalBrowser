from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RedactionRule:
    label: str
    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class RedactionResult:
    text: str
    replacements: int
    labels: tuple[str, ...]


_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "Authorization header": ("Bearer token",),
    "API key assignment": ("credential assignment",),
}

DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        "[REDACTED: PRIVATE KEY]",
    ),
    RedactionRule("AWS access key", re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b"), "[REDACTED: AWS KEY]"),
    RedactionRule("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,255}\b"), "[REDACTED: GITHUB TOKEN]"),
    RedactionRule("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"), "[REDACTED: API KEY]"),
    RedactionRule("Authorization header", re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED: BEARER TOKEN]"),
    RedactionRule("inline bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Bearer [REDACTED: TOKEN]"),
    RedactionRule(
        "API key assignment",
        re.compile(r"(?i)\b(api[_-]?key)\s*[:=]\s*([^\s'\";,]{6,})"),
        r"\1=[REDACTED: VALUE]",
    ),
    RedactionRule(
        "credential assignment",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token)\s*[:=]\s*([^\s'\";,]{6,})"),
        r"\1=[REDACTED: VALUE]",
    ),
    RedactionRule("URL credential", re.compile(r"(?i)(https?://)([^\s:/@]{1,80}):([^\s/@]{3,80})@"), r"\1[REDACTED: USER]:[REDACTED: PASS]@"),
)


def redact_text(text: str, *, rules: Iterable[RedactionRule] = DEFAULT_RULES) -> RedactionResult:
    """Redact common secrets from terminal captures before export/display."""

    output = text
    labels: list[str] = []
    replacements = 0
    for rule in rules:
        output, count = rule.pattern.subn(rule.replacement, output)
        if count:
            replacements += count
            if rule.label not in labels:
                labels.append(rule.label)
            for alias in _LABEL_ALIASES.get(rule.label, ()):
                if alias not in labels:
                    labels.append(alias)
    return RedactionResult(output, replacements, tuple(labels))
