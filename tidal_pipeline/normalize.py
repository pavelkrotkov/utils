"""Shared normalization and low-level text helpers."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional

from tidal_pipeline.models import (
    ENSEMBLE_HINTS,
    GENERIC_ARTIST_TOKENS,
    GENERIC_LINE_PREFIXES,
    INSTRUMENT_ABBREVS,
    INSTRUMENT_MAP,
    SEPARATOR_RE,
    SKIP_ARTIST_SEGMENTS,
    STOPWORDS,
)


def normalize(text: str) -> str:
    if not text:
        return ""
    text = (
        text.replace(".", " ")
        .replace("’", " ")
        .replace("‘", " ")
        .replace("“", " ")
        .replace("”", " ")
        .replace("'", " ")
        .replace('"', " ")
        .replace(":", " ")
        .replace(";", " ")
        .replace(",", " ")
        .replace("+", " ")
        .replace("(", " ")
        .replace(")", " ")
    )
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    text = text.lower()
    text = re.sub(r"[/\\\-&—–]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_with_symbols(text: str) -> str:
    if not text:
        return ""
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
    )
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize(text: str) -> set[str]:
    norm = normalize(text)
    return {t for t in norm.split() if len(t) > 2 and t not in STOPWORDS}


def split_tokens(text: str) -> List[str]:
    norm = normalize(text)
    return [t for t in norm.split() if len(t) > 1 and t not in STOPWORDS]


def tokens_from_list(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(tokenize(value))
    return tokens


def artist_tokens_from_list(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in tokenize(value):
            if token not in GENERIC_ARTIST_TOKENS:
                tokens.add(token)
    return tokens


def extract_instruments(text: str) -> List[str]:
    instruments: List[str] = []
    for token in re.split(r"\s+", text or ""):
        raw = token.strip().strip(",;")
        if raw and raw.lower() in INSTRUMENT_ABBREVS:
            instruments.append(raw)
    return instruments


def normalize_instruments(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for raw in normalize(value).split():
            if raw in GENERIC_LINE_PREFIXES or raw in SKIP_ARTIST_SEGMENTS:
                continue
            tokens.add(INSTRUMENT_MAP.get(raw, raw))
    return {token for token in tokens if token}


def phrase_overlap_score(left_values: Iterable[str], right_values: Iterable[str]) -> float:
    left_norms = [normalize(value) for value in left_values if normalize(value)]
    right_norms = [normalize(value) for value in right_values if normalize(value)]
    if not left_norms:
        return 0.0
    matched = 0
    for left in left_norms:
        if any(left == right or left in right or right in left for right in right_norms):
            matched += 1
    return matched / len(left_norms)


def overlap_score(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    return len(left & right) / len(left)


def extract_year(text: str) -> Optional[str]:
    match = re.search(r"\b(?:19|20)\d{2}\b", text or "")
    return match.group(0) if match else None


def extract_numeric_tokens(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(re.findall(r"\b\d+\b", normalize_with_symbols(value)))
    return tokens


def merge_unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for value in values:
        cleaned = " ".join(str(value or "").split()).strip()
        if not cleaned:
            continue
        key = normalize(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(cleaned)
    return merged


def looks_like_ensemble(text: str) -> bool:
    lowered = normalize(text)
    if not lowered:
        return False
    if any(hint in lowered for hint in ENSEMBLE_HINTS):
        return True
    tokens = lowered.split()
    return any(token in {"rso", "lpo", "rlpo", "bbc"} for token in tokens)


def clean_markdown_inline(text: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = cleaned.replace("*", "").replace("_", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_generic_prefixes(text: str) -> str:
    cleaned = clean_markdown_inline(text)
    lowered = normalize(cleaned)
    for prefix in sorted(GENERIC_LINE_PREFIXES, key=len, reverse=True):
        prefix_norm = normalize(prefix)
        if lowered.startswith(prefix_norm):
            raw_words = cleaned.split()
            prefix_words = prefix_norm.split()
            cleaned = " ".join(raw_words[len(prefix_words) :]).strip(" -–—:;,.")
            lowered = normalize(cleaned)
    return cleaned


def is_markdown_separator(line: str) -> bool:
    return bool(SEPARATOR_RE.fullmatch((line or "").strip()))

