"""Shared source parsing helpers for the TIDAL pipeline."""

from __future__ import annotations

import email
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from tidal_pipeline.normalize import (
    INSTRUMENT_ABBREVS,
    INSTRUMENT_MAP,
    SKIP_ARTIST_SEGMENTS,
    SKIP_SEGMENTS,
    clean_markdown_inline,
    extract_instruments,
    is_markdown_separator,
    looks_like_ensemble,
    merge_unique,
    normalize,
    normalize_instruments,
    strip_generic_prefixes,
    tokens_from_list,
)


@dataclass
class ParsedCandidate:
    work_title_hint: str
    performer_hint: str = ""
    label_hint: str = ""
    title_line: str = ""
    performer_line: str = ""
    label_line: str = ""
    raw_text: str = ""
    subsection: str = ""
    line_number: int | None = None
    source_file: str = ""


def should_skip_candidate(work_title: str) -> bool:
    work = work_title.strip()
    if not work or len(work) < 5:
        return True
    if re.match(r"^\d{4}$", work):
        return True
    return "Related Articles" in work


def add_candidate(
    candidate: ParsedCandidate,
    candidates: list[ParsedCandidate],
    seen_fingerprints: set[tuple[str, str, str]],
) -> None:
    if should_skip_candidate(candidate.work_title_hint):
        return
    fingerprint = (
        normalize(candidate.work_title_hint),
        normalize(candidate.performer_hint),
        normalize(candidate.label_hint),
    )
    if fingerprint in seen_fingerprints:
        return
    seen_fingerprints.add(fingerprint)
    candidates.append(candidate)


def parse_mhtml(file_path: Path) -> tuple[str, list[ParsedCandidate]]:
    with file_path.open("rb") as handle:
        msg = email.message_from_binary_file(handle)

    html_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                try:
                    html_content = payload.decode(charset, errors="replace")
                except Exception:
                    html_content = payload.decode("utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                html_content = payload.decode(charset, errors="replace")
            except Exception:
                html_content = payload.decode("utf-8", errors="replace")
        else:
            html_content = file_path.read_text(encoding="utf-8", errors="replace")

    soup = BeautifulSoup(html_content, "lxml")
    page_title = file_path.stem
    if soup.title:
        page_title = soup.title.string.strip()
    elif soup.find("h1"):
        page_title = soup.find("h1").get_text(strip=True)

    candidates: list[ParsedCandidate] = []
    seen_fingerprints: set[tuple[str, str, str]] = set()

    for header in soup.find_all(["h2", "h3"]):
        text = header.get_text(" ", strip=True)
        if "Related Articles" in text:
            break

        work_title = text
        next_elem = header.find_next_sibling()
        performer_hint = ""
        label_hint = ""
        performer_line = ""
        label_line = ""
        last_performer_line = ""

        dist = 0
        while next_elem and dist < 5:
            if next_elem.name in ["h2", "h3", "hr"]:
                break

            line_text = next_elem.get_text(" ", strip=True)
            if not line_text:
                next_elem = next_elem.find_next_sibling()
                continue

            label_match = re.search(r"\(([^)]+)\)$", line_text)
            label_text = ""
            if label_match:
                label_text = label_match.group(1)
            elif "label:" in line_text.lower():
                label_text = line_text.split(":", 1)[-1].strip()

            if label_text:
                label_hint = label_text
                label_line = line_text
                performer_candidate = (
                    line_text[: label_match.start()].strip() if label_match else ""
                )
                if performer_candidate:
                    performer_hint = performer_candidate
                    performer_line = line_text
                elif last_performer_line:
                    performer_hint = last_performer_line
                    performer_line = last_performer_line
                break

            if next_elem.name == "p" and (next_elem.find("strong") or next_elem.find("em")):
                last_performer_line = line_text

            next_elem = next_elem.find_next_sibling()
            dist += 1

        if not performer_hint and last_performer_line:
            performer_hint = last_performer_line
            performer_line = last_performer_line

        add_candidate(
            ParsedCandidate(
                work_title_hint=work_title,
                performer_hint=performer_hint,
                label_hint=label_hint,
                title_line=work_title,
                performer_line=performer_line,
                label_line=label_line,
                raw_text=text[:200],
                subsection="\n".join(
                    segment for segment in [work_title, performer_line, label_line] if segment
                ),
                source_file=file_path.name,
            ),
            candidates,
            seen_fingerprints,
        )

    return page_title, candidates


def is_review_line(line: str) -> bool:
    lowered = line.lower()
    return "/review/" in lowered and "](" in line


def is_markdown_image_heading(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("##") and "![](" in stripped


def clean_markdown_heading(line: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", line).strip()
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def candidate_from_markdown_block(
    block: list[tuple[int, str]],
    file_path: Path,
) -> ParsedCandidate | None:
    review_pos = next((idx for idx, (_, line) in enumerate(block) if is_review_line(line)), -1)
    if review_pos < 0:
        return None

    title_entries: list[tuple[int, str, str]] = []
    for line_number, line in block[: review_pos + 1]:
        stripped = line.strip()
        if not stripped.startswith("##"):
            continue
        if is_markdown_image_heading(stripped):
            continue
        if "Related Reviews" in stripped or "Related News" in stripped:
            continue
        cleaned = clean_markdown_heading(stripped)
        if cleaned:
            title_entries.append((line_number, stripped, cleaned))

    if not title_entries:
        return None

    line_number, title_line, work_title = title_entries[-1]
    if should_skip_candidate(work_title):
        return None

    performer_hint = ""
    label_hint = ""
    performer_line = ""
    label_line = ""
    in_detail_section = False

    for _, raw_line in block:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("##") and clean_markdown_heading(stripped) == work_title:
            in_detail_section = True
            continue
        if not in_detail_section:
            continue
        if is_review_line(stripped) or is_markdown_separator(stripped):
            break
        if stripped.startswith("##"):
            break
        if stripped.lower().startswith("label:"):
            if not label_hint:
                label_hint = stripped.split(":", 1)[1].strip()
                label_line = stripped
            continue
        if not performer_line:
            performer_line = stripped
            clean_perf = re.sub(r"[*_]", "", stripped).strip()
            label_match = re.search(r"\(([^)]+)\)\s*$", clean_perf)
            if label_match:
                label_hint = label_match.group(1).strip()
                label_line = stripped
                clean_perf = clean_perf[: label_match.start()].strip()
            performer_hint = clean_perf

    if not performer_hint and not label_hint:
        return None

    return ParsedCandidate(
        work_title_hint=work_title,
        performer_hint=performer_hint,
        label_hint=label_hint,
        title_line=title_line,
        performer_line=performer_line,
        label_line=label_line,
        raw_text=work_title,
        subsection="\n".join(line for _, line in block).strip(),
        line_number=line_number,
        source_file=file_path.name,
    )


def parse_markdown_blocks(file_path: Path) -> tuple[str, list[ParsedCandidate]]:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    page_title = file_path.stem
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            page_title = stripped[2:].strip()
            break
        if stripped.startswith("## ") and not is_markdown_image_heading(stripped):
            cleaned = clean_markdown_heading(stripped)
            if cleaned:
                page_title = cleaned
                break

    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("## Related Reviews") or stripped.startswith("## Related News"):
            break
        if is_markdown_separator(stripped):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append((line_number, line))
    if current:
        blocks.append(current)

    candidates: list[ParsedCandidate] = []
    seen_fingerprints: set[tuple[str, str, str]] = set()
    for block in blocks:
        candidate = candidate_from_markdown_block(block, file_path)
        if candidate:
            add_candidate(candidate, candidates, seen_fingerprints)

    return page_title, candidates


def parse_markdown_legacy(file_path: Path) -> tuple[str, list[ParsedCandidate]]:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    page_title = file_path.stem
    for line in lines:
        if line.strip().startswith("# "):
            page_title = line.strip()[2:].strip()
            break

    candidates: list[ParsedCandidate] = []
    seen_fingerprints: set[tuple[str, str, str]] = set()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if "![Image" in line or line.startswith("**Source:**") or line.startswith("**!["):
            continue

        if line.startswith("**"):
            raw_text = line
            has_separator = False
            if i < len(lines) and re.match(r"^[-=]{3,}$", lines[i].strip()):
                has_separator = True

            work_title = line.replace("**", " ").strip()
            work_title = re.sub(r"\s+", " ", work_title)
            if len(work_title) < 5 or re.match(r"^\d{4}$", work_title):
                continue

            if not has_separator:
                if re.search(r"\([^)]+\)$", line):
                    continue
                is_likely_performer = False
                for j in range(i, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    if not next_line:
                        continue
                    if re.search(r"\(([^)]+)\)$", next_line):
                        is_likely_performer = True
                        break
                if is_likely_performer:
                    continue

            if has_separator:
                i += 1

            performer_hint = ""
            label_hint = ""
            performer_line = ""
            label_line = ""
            last_performer_line = ""
            look_ahead_limit = 6
            for offset in range(look_ahead_limit):
                idx = i + offset
                if idx >= len(lines):
                    break
                next_line = lines[idx].strip()
                if not next_line:
                    continue
                if next_line.startswith("---") or next_line.startswith("**20"):
                    break
                is_next_header = False
                if idx + 1 < len(lines) and re.match(r"^[-=]{3,}$", lines[idx + 1].strip()):
                    is_next_header = True
                if is_next_header:
                    break

                if next_line.startswith("**"):
                    clean_perf = next_line.replace("**", "").replace("__", "")
                    label_match = re.search(r"\(([^)]+)\)$", clean_perf)
                    if label_match:
                        label_hint = label_match.group(1)
                        label_line = next_line
                        performer_candidate = clean_perf[: label_match.start()].strip()
                        if performer_candidate:
                            performer_hint = performer_candidate
                            performer_line = next_line
                        elif last_performer_line:
                            performer_hint = last_performer_line
                            performer_line = last_performer_line
                        break
                    else:
                        last_performer_line = clean_perf
                        performer_line = next_line
                elif next_line.lower().startswith("label:"):
                    label_hint = next_line.split(":", 1)[-1].strip()
                    label_line = next_line
                    if last_performer_line:
                        performer_hint = last_performer_line
                    break

            if performer_hint or label_hint:
                add_candidate(
                    ParsedCandidate(
                        work_title_hint=work_title,
                        performer_hint=performer_hint,
                        label_hint=label_hint,
                        title_line=raw_text,
                        performer_line=performer_line,
                        label_line=label_line,
                        raw_text=raw_text[:200],
                        subsection="\n".join(
                            segment for segment in [raw_text, performer_line, label_line] if segment
                        ),
                        line_number=i,
                        source_file=file_path.name,
                    ),
                    candidates,
                    seen_fingerprints,
                )

    return page_title, candidates


def parse_markdown(file_path: Path) -> tuple[str, list[ParsedCandidate]]:
    page_title, candidates = parse_markdown_blocks(file_path)
    if candidates:
        return page_title, candidates
    return parse_markdown_legacy(file_path)


def extract_candidates(file_path: Path) -> tuple[str, list[ParsedCandidate]]:
    ext = file_path.suffix.lower()
    if ext in {".mhtml", ".mht"}:
        return parse_mhtml(file_path)
    if ext in {".md", ".markdown"}:
        return parse_markdown(file_path)
    raise ValueError(f"Unsupported file extension: {ext}")


def split_performer_hint(text: str) -> tuple[list[str], list[str], str, list[str]]:
    if not text:
        return [], [], "", []

    instruments = extract_instruments(text)
    conductor = ""
    parts = [part.strip() for part in text.split("/") if part.strip()]
    left = text
    if len(parts) > 1:
        candidate = parts[-1]
        left_candidate = " / ".join(parts[:-1]).strip()
        if candidate and not looks_like_ensemble(candidate):
            conductor = candidate
            left = left_candidate

    performers: list[str] = []
    ensembles: list[str] = []

    if instruments:
        tokens = [token.strip(",;") for token in left.split() if token.strip(",;")]
        current: list[str] = []
        for idx, token in enumerate(tokens):
            lower = token.lower()
            if lower in INSTRUMENT_ABBREVS:
                if current:
                    performers.append(" ".join(current))
                    current = []
                continue
            if looks_like_ensemble(token):
                ensemble_tokens = current + tokens[idx:]
                ensemble = " ".join(ensemble_tokens).strip()
                if ensemble:
                    ensembles.append(ensemble)
                current = []
                break
            current.append(token)
        if current:
            performers.append(" ".join(current))
        return performers, ensembles, conductor, instruments

    segments: list[str] = []
    for chunk in left.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        segments.extend([part.strip() for part in chunk.split(",") if part.strip()])

    for segment in segments:
        if segment.lower() in SKIP_SEGMENTS:
            continue
        if looks_like_ensemble(segment):
            ensembles.append(segment)
        else:
            if ensembles and len(segment.split()) <= 2:
                ensembles[-1] = f"{ensembles[-1]}, {segment}"
            else:
                performers.append(segment)

    return performers, ensembles, conductor, instruments


def parse_heading_metadata(heading_line: str, fallback_title: str) -> tuple[list[str], str, str]:
    cleaned_heading = clean_markdown_inline(re.sub(r"^#{1,6}\s*", "", heading_line or "")).strip()
    bold_segments = [
        clean_markdown_inline(seg) for seg in re.findall(r"\*\*(.+?)\*\*", heading_line or "")
    ]
    composers: list[str] = []
    for segment in bold_segments:
        composers.extend(part.strip() for part in re.split(r"\s*[.;/]\s*", segment) if part.strip())
    composers = merge_unique(composers)

    remainder = re.sub(r"\*\*.+?\*\*", " ", re.sub(r"^#{1,6}\s*", "", heading_line or ""))
    remainder = clean_markdown_inline(remainder).strip(" -–—:;,.")
    if not composers and cleaned_heading:
        words = cleaned_heading.split()
        composer_words: list[str] = []
        for word in words:
            stripped = re.sub(r"[^A-Za-z.]", "", word)
            if not stripped:
                break
            if stripped.isupper():
                composer_words.append(word)
                continue
            break
        if composer_words:
            composers = merge_unique([" ".join(composer_words)])
            remainder = cleaned_heading[len(" ".join(composer_words)) :].strip(" -–—:;,.")
    if not remainder:
        remainder = fallback_title

    return composers, remainder, cleaned_heading or fallback_title


def extract_review_slug(subsection: str) -> str:
    match = re.search(r"/review/([^)\s]+)", subsection or "", flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def extract_italicized_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for match in re.findall(r"_([^_]+)_", text or ""):
        cleaned = clean_markdown_inline(match).strip(" -–—:;,.")
        if normalize(cleaned) in {"gramophone", "review"}:
            continue
        if cleaned and len(cleaned) > 2:
            phrases.append(cleaned)
    return merge_unique(phrases)


def build_slug_phrases(slug: str, remove_terms: list[str]) -> list[str]:
    if not slug:
        return []
    tokens = [token for token in slug.replace("-", " ").split() if token]
    remove = tokens_from_list(remove_terms)
    pruned = [token for token in tokens if normalize(token) not in remove]
    phrases = [" ".join(tokens).strip()]
    if pruned and pruned != tokens:
        phrases.append(" ".join(pruned).strip())
    return merge_unique(phrases)


def strip_instrument_tokens(text: str, instruments: list[str]) -> str:
    lowered_instruments = {normalize(value) for value in instruments if value}
    tokens: list[str] = []
    for token in clean_markdown_inline(text).split():
        norm = normalize(token)
        mapped = normalize(INSTRUMENT_MAP.get(norm, norm))
        if norm in lowered_instruments or mapped in lowered_instruments:
            continue
        tokens.append(token)
    return " ".join(tokens).strip(" -–—:;,.")


def prune_artist_values(
    values: list[str],
    peer_values: list[str],
    instruments: list[str],
) -> list[str]:
    peers = [normalize(value) for value in peer_values if value]
    instrument_tokens = {normalize(value) for value in instruments if value}
    kept: list[str] = []
    for value in merge_unique(values):
        norm = normalize(value)
        if not norm:
            continue
        if any(token in instrument_tokens for token in norm.split()) and any(
            peer and peer in norm and peer != norm for peer in peers
        ):
            continue
        if any(peer and peer in norm and peer != norm for peer in peers):
            continue
        kept.append(value)
    return merge_unique(kept)


def prune_composer_values(values: list[str], title: str, full_title: str) -> list[str]:
    kept: list[str] = []
    title_norm = normalize(title)
    full_title_norm = normalize(full_title)
    for value in merge_unique(values):
        norm = normalize(value)
        if not norm:
            continue
        if norm == title_norm:
            continue
        if full_title_norm and norm == full_title_norm:
            continue
        kept.append(value)
    return merge_unique(kept)


def parse_performer_metadata(
    performer_line: str,
    label_line: str,
) -> tuple[list[str], list[str], str, list[str], str]:
    raw_line = (performer_line or "").strip()
    label = ""
    match = re.search(r"\(([^)]+)\)\s*$", raw_line or label_line or "")
    if match:
        label = match.group(1).strip()

    instruments = sorted(normalize_instruments(re.findall(r"_([^_]+)_", raw_line)))
    cleaned_line = raw_line
    if match and raw_line:
        cleaned_line = raw_line[: match.start()].strip()

    bold_segments = [
        strip_generic_prefixes(seg.strip()) for seg in re.findall(r"\*\*(.+?)\*\*", cleaned_line)
    ]
    bold_segments = [segment for segment in bold_segments if segment]

    performers: list[str] = []
    ensembles: list[str] = []
    conductor_parts: list[str] = []

    if len(bold_segments) > 1:
        for segment in bold_segments:
            cleaned_segment = strip_instrument_tokens(segment, instruments)
            if not cleaned_segment:
                continue
            if "/" in cleaned_segment:
                left_side, right_side = [part.strip() for part in cleaned_segment.split("/", 1)]
                if left_side:
                    for idx, part in enumerate(
                        [item.strip() for item in re.split(r"\s*;\s*", left_side) if item.strip()]
                    ):
                        cleaned = strip_instrument_tokens(strip_generic_prefixes(part), instruments)
                        if not cleaned or normalize(cleaned) in SKIP_ARTIST_SEGMENTS:
                            continue
                        if "&" in cleaned and idx == 0:
                            performers.extend(
                                piece.strip() for piece in cleaned.split("&") if piece.strip()
                            )
                            continue
                        ensembles.append(cleaned)
                if right_side:
                    for part in [
                        item.strip()
                        for item in re.split(r"\s*;\s*|\s*,\s*", right_side)
                        if item.strip()
                    ]:
                        cleaned = strip_instrument_tokens(strip_generic_prefixes(part), instruments)
                        if cleaned and normalize(cleaned) not in SKIP_ARTIST_SEGMENTS:
                            conductor_parts.append(cleaned)
                continue

            cleaned = strip_instrument_tokens(cleaned_segment, instruments)
            if not cleaned or normalize(cleaned) in SKIP_ARTIST_SEGMENTS:
                continue
            if looks_like_ensemble(cleaned):
                ensembles.append(cleaned)
            else:
                performers.append(cleaned)
    else:
        plain_line = strip_generic_prefixes(clean_markdown_inline(cleaned_line))
        left_side = plain_line
        right_side = ""
        if "/" in plain_line:
            left_side, right_side = [part.strip() for part in plain_line.split("/", 1)]

        left_segments = [part.strip() for part in re.split(r"\s*;\s*", left_side) if part.strip()]
        for idx, segment in enumerate(left_segments):
            cleaned = strip_instrument_tokens(strip_generic_prefixes(segment), instruments)
            if not cleaned or normalize(cleaned) in SKIP_ARTIST_SEGMENTS:
                continue
            if "&" in cleaned and not looks_like_ensemble(cleaned):
                performers.extend(part.strip() for part in cleaned.split("&") if part.strip())
                continue
            if right_side and (idx > 0 or (len(left_segments) == 1 and len(cleaned.split()) <= 4)):
                ensembles.append(cleaned)
                continue
            if looks_like_ensemble(cleaned):
                ensembles.append(cleaned)
            else:
                performers.append(cleaned)

        if right_side:
            for segment in [
                part.strip() for part in re.split(r"\s*,\s*", right_side) if part.strip()
            ]:
                cleaned = strip_instrument_tokens(strip_generic_prefixes(segment), instruments)
                if not cleaned or normalize(cleaned) in SKIP_ARTIST_SEGMENTS:
                    continue
                conductor_parts.append(cleaned)

    conductor = "; ".join(merge_unique(conductor_parts))
    if not label and label_line.lower().startswith("label:"):
        label = clean_markdown_inline(
            re.sub(r"^label:\s*", "", label_line, flags=re.IGNORECASE)
        ).strip("()")

    performers = prune_artist_values(
        performers, ensembles + ([conductor] if conductor else []), instruments
    )
    ensembles = prune_artist_values(
        ensembles, performers + ([conductor] if conductor else []), instruments
    )
    return performers, ensembles, conductor, instruments, label


def candidate_to_entry(candidate: ParsedCandidate) -> dict:
    composers, title, full_title = parse_heading_metadata(
        candidate.title_line or candidate.work_title_hint,
        candidate.work_title_hint,
    )

    performers, ensembles, conductor, instruments, parsed_label = parse_performer_metadata(
        candidate.performer_line or candidate.performer_hint,
        candidate.label_line or (f"({candidate.label_hint})" if candidate.label_hint else ""),
    )
    if not performers and not ensembles and not conductor:
        performers, ensembles, conductor, fallback_instruments = split_performer_hint(
            candidate.performer_hint
        )
        if not instruments:
            instruments = fallback_instruments

    subsection = candidate.subsection or candidate.raw_text or candidate.work_title_hint
    review_slug = extract_review_slug(subsection)
    slug_phrases = build_slug_phrases(review_slug, [candidate.work_title_hint, title, *composers])
    prose_work_hints = extract_italicized_phrases(subsection)
    works = merge_unique(
        [
            title,
            candidate.work_title_hint,
            *slug_phrases,
            *prose_work_hints,
        ]
    )
    composers = prune_composer_values(composers, title, full_title)

    label = parsed_label or candidate.label_hint
    return {
        "source": {
            "file": candidate.source_file,
            "line": candidate.line_number or 0,
            "raw": candidate.raw_text or subsection or candidate.work_title_hint,
            "subsection": subsection,
            "context": {
                "title_line": candidate.title_line or candidate.work_title_hint,
                "performer_line": candidate.performer_line or candidate.performer_hint,
                "label_line": candidate.label_line
                or (f"({candidate.label_hint})" if candidate.label_hint else ""),
            },
        },
        "album": {
            "title": title,
            "composers": composers,
            "performers": performers,
            "ensembles": ensembles,
            "conductor": conductor,
            "label": label,
            "year": "",
            "works": works or ([title] if title else []),
            "instruments": instruments,
        },
    }


def parse_file_to_entries(file_path: Path) -> list[dict]:
    _, candidates = extract_candidates(file_path)
    return [candidate_to_entry(candidate) for candidate in candidates]
