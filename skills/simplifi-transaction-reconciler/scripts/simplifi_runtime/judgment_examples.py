"""Load the explicitly promoted, sanitized judgment examples.

Transaction history is evidence for one run, not reusable training material.
This module has one deliberately narrow input: the packaged Markdown file under
``references/examples``. It parses only the documented headings and returns
plain, packet-safe dictionaries for prompts and review packets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_EXAMPLES_PATH = (
    Path(__file__).resolve().parents[2] / "references" / "examples" / "judgment-examples.md"
)

_FIELD_NAMES = {
    "Situation": "situation",
    "Evidence": "evidence",
    "Proposal or escalation": "proposal_or_escalation",
    "Human decision": "human_decision",
    "Reusable lesson": "reusable_lesson",
}
_REQUIRED_FIELDS = tuple(_FIELD_NAMES.values())
_EXAMPLE_HEADING = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_FIELD_HEADING = re.compile(r"^\*\*(.+?)\*\*\s*$")
_FORBIDDEN_TERMS = re.compile(
    r"\b(?:access_token|account_id|account_name|client_secret|cookie|password|"
    r"payee_raw|raw_descriptor|session_token|source_hash|transaction_id|"
    r"transaction_ids)\b",
    re.IGNORECASE,
)


class JudgmentExampleError(ValueError):
    """Raised when the promoted judgment examples are malformed or unsafe."""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _validate_text(value: str, path: str) -> str:
    value = _clean(value)
    if not value:
        raise JudgmentExampleError(f"{path} is empty")
    match = _FORBIDDEN_TERMS.search(value)
    if match:
        raise JudgmentExampleError(f"{path} contains forbidden term {match.group(0)!r}")
    return value


def _parse_block(number: str, title: str, lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    values: list[str] = []
    for line in lines:
        heading = _FIELD_HEADING.match(line.strip())
        if heading:
            if current is not None:
                fields[current] = _validate_text(" ".join(values), f"example {number}.{current}")
            label = heading.group(1)
            if label not in _FIELD_NAMES:
                raise JudgmentExampleError(f"example {number} has unsupported field {label!r}")
            current = _FIELD_NAMES[label]
            values = []
            continue
        if line.startswith("#"):
            raise JudgmentExampleError(f"example {number} contains an unsupported heading")
        if current is not None:
            values.append(line)

    if current is not None:
        fields[current] = _validate_text(" ".join(values), f"example {number}.{current}")
    missing = [field for field in _REQUIRED_FIELDS if field not in fields]
    if missing:
        raise JudgmentExampleError(f"example {number} is missing fields: {', '.join(missing)}")
    return {
        "id": f"judgment-{int(number)}",
        "title": _validate_text(title, f"example {number}.title"),
        **{field: fields[field] for field in _REQUIRED_FIELDS},
    }


def load_curated_examples(path: Path | None = None) -> list[dict[str, str]]:
    """Load only the explicitly promoted examples from the packaged reference."""
    path = Path(path or DEFAULT_EXAMPLES_PATH)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JudgmentExampleError(f"cannot read curated examples at {path}") from exc

    matches = list(re.finditer(r"(?m)^##\s+", text))
    examples: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start() : end]
        heading, *body = block.splitlines()
        parsed = _EXAMPLE_HEADING.match(heading)
        if not parsed:
            raise JudgmentExampleError(f"unsupported second-level heading: {heading!r}")
        examples.append(_parse_block(parsed.group(1), parsed.group(2), body))

    if not examples:
        raise JudgmentExampleError("curated examples file contains no examples")
    numbers = [example["id"] for example in examples]
    if len(numbers) != len(set(numbers)):
        raise JudgmentExampleError("curated examples contain duplicate numbers")
    return examples


_STOP_WORDS = {
    "a",
    "and",
    "an",
    "as",
    "before",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def select_relevant_examples(
    examples: Iterable[dict[str, str]], context: str, limit: int = 5
) -> list[dict[str, str]]:
    """Rank examples by deterministic topic overlap, retaining stable ordering.

    A context with no distinctive overlap returns the complete small curated set
    up to ``limit``. That keeps general read-only lessons available without
    falling back to transaction history or silently inventing relevance.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    context_tokens = _tokens(context)
    scored = []
    for position, example in enumerate(examples):
        example_tokens = _tokens(" ".join(example[field] for field in ("title", *_REQUIRED_FIELDS)))
        score = len(context_tokens & example_tokens)
        scored.append((-score, position, example))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [example for _, _, example in scored[:limit]]


def context_from_review(
    prioritized: Iterable[Any],
    subscription_findings: Iterable[Any],
    proposals: Iterable[tuple[dict[str, Any], Any]],
) -> str:
    """Build a non-persistent topic string from the current run's findings."""
    parts = ["read-only transaction review"]
    for item in prioritized:
        parts.extend(str(signal.name) for signal in item.signals)
    for finding in subscription_findings:
        parts.extend([str(finding.kind), str(finding.detail)])
    for row, proposal in proposals:
        parts.append(str(row.get("category") or "uncategorized"))
        if proposal is not None:
            parts.append(str(proposal.evidence))
    return " ".join(parts)
