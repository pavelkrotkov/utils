#!/usr/bin/env python3
# /// script
# dependencies = [
#   "beautifulsoup4",
#   "lxml",
# ]
# ///
"""Parse Gramophone-style MHTML/Markdown into structured JSON."""

from __future__ import annotations

import argparse
import email
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup


ENSEMBLE_KEYWORDS = [
    "orchestra",
    "philharmonic",
    "symphony",
    "ensemble",
    "quartet",
    "trio",
    "quintet",
    "choir",
    "chorus",
    "consort",
    "players",
    "sinfonia",
    "academy",
    "capella",
    "coro",
    "chamber",
    "opera",
    "filharmonica",
    "radio",
]

INSTRUMENT_ABBREVS = {
    "pf",
    "vn",
    "va",
    "vc",
    "db",
    "fl",
    "ob",
    "cl",
    "bn",
    "hn",
    "tpt",
    "trb",
    "hp",
    "org",
    "hpd",
    "pno",
    "gtr",
    "perc",
    "sop",
    "mez",
    "ten",
    "bar",
    "bass",
}

SKIP_SEGMENTS = {"sols", "sols.", "soloists", "soloist"}


@dataclass
class Candidate:
    work_title_hint: str
    performer_hint: str = ""
    label_hint: str = ""
    performer_line: str = ""
    label_line: str = ""
    raw_text: str = ""
    line_number: Optional[int] = None
    source_file: str = ""


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    return text.lower()


def should_skip_candidate(work_title: str) -> bool:
    work = work_title.strip()
    if not work or len(work) < 5:
        return True
    if re.match(r"^\d{4}$", work):
        return True
    if "Related Articles" in work:
        return True
    return False


def add_candidate(cand: Candidate, candidates: List[Candidate], seen_fingerprints: set) -> None:
    if should_skip_candidate(cand.work_title_hint):
        return
    fp = (
        normalize(cand.work_title_hint),
        normalize(cand.performer_hint),
        normalize(cand.label_hint),
    )
    if fp in seen_fingerprints:
        return
    seen_fingerprints.add(fp)
    candidates.append(cand)


def parse_mhtml(file_path: Path) -> Tuple[str, List[Candidate]]:
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

    candidates: List[Candidate] = []
    seen_fingerprints: set = set()

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

            p_text = next_elem.get_text(" ", strip=True)
            if not p_text:
                next_elem = next_elem.find_next_sibling()
                continue

            label_match = re.search(r"\(([^)]+)\)$", p_text)
            label_text = ""
            if label_match:
                label_text = label_match.group(1)
            elif "label:" in p_text.lower():
                label_text = p_text.split(":", 1)[-1].strip()

            if label_text:
                label_hint = label_text
                label_line = p_text
                performer_candidate = p_text[: label_match.start()].strip() if label_match else ""
                if performer_candidate:
                    performer_hint = performer_candidate
                    performer_line = p_text
                elif last_performer_line:
                    performer_hint = last_performer_line
                    performer_line = last_performer_line
                break

            if next_elem.name == "p" and (next_elem.find("strong") or next_elem.find("em")):
                last_performer_line = p_text

            next_elem = next_elem.find_next_sibling()
            dist += 1

        if not performer_hint and last_performer_line:
            performer_hint = last_performer_line
            performer_line = last_performer_line

        add_candidate(
            Candidate(
                work_title_hint=work_title,
                performer_hint=performer_hint,
                label_hint=label_hint,
                performer_line=performer_line,
                label_line=label_line,
                raw_text=text[:200],
                source_file=file_path.name,
            ),
            candidates,
            seen_fingerprints,
        )

    return page_title, candidates


def parse_markdown(file_path: Path) -> Tuple[str, List[Candidate]]:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    page_title = file_path.stem
    for line in lines:
        if line.strip().startswith("# "):
            page_title = line.strip()[2:].strip()
            break

    candidates: List[Candidate] = []
    seen_fingerprints: set = set()

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
                    nl = lines[j].strip()
                    if not nl:
                        continue
                    if re.search(r"\(([^)]+)\)$", nl):
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
                        if not label_hint:
                            label_hint = label_match.group(1)
                            label_line = next_line
                        clean_perf = clean_perf[: label_match.start()]
                    if not performer_hint:
                        performer_hint = clean_perf.strip()
                        performer_line = next_line
                    else:
                        performer_hint += " " + clean_perf.strip()
                else:
                    label_match = re.search(r"\(([^)]+)\)$", next_line)
                    if label_match:
                        if not label_hint:
                            label_hint = label_match.group(1)
                            label_line = next_line
                        pre_label = next_line[: label_match.start()].strip()
                        if pre_label and not performer_hint:
                            performer_hint = pre_label
                            performer_line = pre_label

                    if not label_match and ("**" in next_line or "_" in next_line):
                        last_performer_line = next_line

            if label_hint and not performer_hint and last_performer_line:
                performer_hint = last_performer_line
                performer_line = last_performer_line

            performer_hint = re.sub(r"[*_]", "", performer_hint).strip()

            add_candidate(
                Candidate(
                    work_title_hint=work_title,
                    performer_hint=performer_hint,
                    label_hint=label_hint,
                    performer_line=performer_line,
                    label_line=label_line,
                    raw_text=raw_text[:200],
                    line_number=i - 1,
                    source_file=file_path.name,
                ),
                candidates,
                seen_fingerprints,
            )

    return page_title, candidates


def extract_candidates(file_path: Path) -> Tuple[str, List[Candidate]]:
    ext = file_path.suffix.lower()
    if ext in {".mhtml", ".mht"}:
        return parse_mhtml(file_path)
    if ext in {".md", ".markdown"}:
        return parse_markdown(file_path)
    raise ValueError(f"Unsupported file extension: {ext}")


def extract_instruments(text: str) -> List[str]:
    instruments: List[str] = []
    for token in re.split(r"\s+", text):
        raw = token.strip().strip(",;")
        if raw and raw.lower() in INSTRUMENT_ABBREVS:
            instruments.append(raw)
    return instruments


def is_ensemble(segment: str) -> bool:
    lowered = segment.lower()
    return any(keyword in lowered for keyword in ENSEMBLE_KEYWORDS)


def split_performer_hint(text: str) -> Tuple[List[str], List[str], str, List[str]]:
    if not text:
        return [], [], "", []

    instruments = extract_instruments(text)
    conductor = ""

    parts = [p.strip() for p in text.split("/") if p.strip()]
    left = text
    if len(parts) > 1:
        candidate = parts[-1]
        left_candidate = " / ".join(parts[:-1]).strip()
        if candidate and not is_ensemble(candidate):
            conductor = candidate
            left = left_candidate

    performers: List[str] = []
    ensembles: List[str] = []

    if instruments:
        tokens = [tok.strip(",;") for tok in left.split() if tok.strip(",;")]
        current: List[str] = []
        for idx, token in enumerate(tokens):
            lower = token.lower()
            if lower in INSTRUMENT_ABBREVS:
                if current:
                    performers.append(" ".join(current))
                    current = []
                continue
            if any(keyword in lower for keyword in ENSEMBLE_KEYWORDS):
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

    segments: List[str] = []
    for chunk in left.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        segments.extend([part.strip() for part in chunk.split(",") if part.strip()])

    for segment in segments:
        if segment.lower() in SKIP_SEGMENTS:
            continue
        if is_ensemble(segment):
            ensembles.append(segment)
        else:
            if ensembles and len(segment.split()) <= 2:
                ensembles[-1] = f"{ensembles[-1]}, {segment}"
            else:
                performers.append(segment)

    return performers, ensembles, conductor, instruments


def candidate_to_entry(candidate: Candidate) -> dict:
    performers, ensembles, conductor, instruments = split_performer_hint(candidate.performer_hint)
    return {
        "source": {
            "file": candidate.source_file,
            "line": candidate.line_number or 0,
            "raw": candidate.raw_text or candidate.work_title_hint,
            "context": {
                "title_line": candidate.work_title_hint,
                "performer_line": candidate.performer_line or candidate.performer_hint,
                "label_line": candidate.label_line
                or (f"({candidate.label_hint})" if candidate.label_hint else ""),
            },
        },
        "album": {
            "title": candidate.work_title_hint,
            "composers": [],
            "performers": performers,
            "ensembles": ensembles,
            "conductor": conductor,
            "label": candidate.label_hint,
            "year": "",
            "works": [],
            "instruments": instruments,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse Gramophone-style MHTML/Markdown into structured JSON.",
    )
    parser.add_argument("input_path", type=Path, help="Input .mhtml or .md file")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file (defaults to <input>.albums.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input_path.exists():
        raise SystemExit(f"Input file not found: {args.input_path}")

    _, candidates = extract_candidates(args.input_path)
    entries = [candidate_to_entry(cand) for cand in candidates]

    output_path = args.output or args.input_path.with_suffix(".albums.json")
    output_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(entries)} albums to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
