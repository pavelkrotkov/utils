#!/usr/bin/env python3
"""Shared helpers for PDF conversion scripts."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType

MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HTML_IMG_SRC_RE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.IGNORECASE)


def collect_local_references(markdown_text: str) -> list[str]:
    """Return unique local file references from Markdown links and image tags."""
    references: list[str] = []
    seen: set[str] = set()
    matches = list(MARKDOWN_LINK_RE.finditer(markdown_text))
    matches.extend(HTML_IMG_SRC_RE.finditer(markdown_text))

    for match in matches:
        raw_reference = urllib.parse.unquote(match.group(1))
        reference = raw_reference.split("#", 1)[0].split("?", 1)[0]
        if not reference or reference in seen:
            continue
        if reference.lower().startswith(("http://", "https://", "data:", "file:")):
            continue
        seen.add(reference)
        references.append(reference)
    return references


def copy_referenced_assets(
    markdown_text: str,
    source_dir: Path,
    target_dir: Path,
) -> list[Path]:
    """Copy files referenced by relative links next to the output Markdown.

    Returns the list of copied paths. References outside ``source_dir`` are
    ignored, and existing files at the destination are left untouched.
    """
    copied: list[Path] = []
    resolved_source_dir = source_dir.resolve()

    for reference in collect_local_references(markdown_text):
        candidate = (source_dir / reference).resolve()
        try:
            relative_path = candidate.relative_to(resolved_source_dir)
        except ValueError:
            continue
        if not candidate.is_file():
            continue

        target_path = target_dir / relative_path
        if target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target_path)
        copied.append(target_path)
    return copied


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    """Resolve a Markdown output path and ensure its parent directory exists."""
    if output_path is not None:
        if output_path.resolve() == input_path.resolve():
            print(
                f"ERROR: Output path must differ from the input PDF: {output_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    resolved_dir = output_dir or input_path.parent
    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir / f"{input_path.stem}.md"


def require_pdf_path(pdf_path: str | Path) -> Path:
    """Return a valid PDF path or exit with a CLI-oriented error."""
    resolved_path = Path(pdf_path)
    if not resolved_path.exists() or not resolved_path.is_file():
        print(f"ERROR: PDF file not found: {resolved_path}", file=sys.stderr)
        sys.exit(1)
    if resolved_path.suffix.lower() != ".pdf":
        print(f"ERROR: Input file must be a PDF: {resolved_path}", file=sys.stderr)
        sys.exit(1)
    return resolved_path


def import_or_die(module_name: str, install_package: str) -> ModuleType:
    """Import an optional dependency or exit with a consistent install hint."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print(f"Install with: pip install {install_package}", file=sys.stderr)
        sys.exit(1)


def collapse_consecutive(pages: list[int]) -> list[tuple[int, int]]:
    """Collapse page numbers into inclusive consecutive ranges."""
    if not pages:
        return []

    sorted_pages = sorted(set(pages))
    ranges: list[tuple[int, int]] = []
    start = sorted_pages[0]
    previous = sorted_pages[0]

    for page in sorted_pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((start, previous))
        start = page
        previous = page

    ranges.append((start, previous))
    return ranges


def format_page_ranges(ranges: list[tuple[int, int]]) -> str:
    """Format inclusive page ranges as a comma-separated API page spec."""
    return ",".join(f"{start}-{end}" if start != end else str(start) for start, end in ranges)


def parse_page_token(token: str, page_count: int, *, one_based: bool) -> int:
    if token.lower() == "n":
        page = page_count
    else:
        try:
            page = int(token)
        except ValueError as exc:
            raise ValueError(
                f"Invalid --page-range value: {token!r} is not a page number."
            ) from exc

    if page < 1:
        raise ValueError("Invalid --page-range value: pages are 1-based.")
    if page > page_count:
        raise ValueError("Invalid --page-range value: page is outside the document.")
    return page if one_based else page - 1


def parse_page_range(spec: str, page_count: int, *, one_based: bool) -> list[int]:
    if page_count < 1:
        raise ValueError("Invalid --page-range value: PDF has no pages.")

    pages: list[int] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError("Invalid --page-range value: empty page range item.")
        if part.count("-") > 1:
            raise ValueError("Invalid --page-range value: malformed page range.")
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            if not start_raw.strip() or not end_raw.strip():
                raise ValueError("Invalid --page-range value: malformed page range.")
            start = parse_page_token(start_raw.strip(), page_count, one_based=one_based)
            end = parse_page_token(end_raw.strip(), page_count, one_based=one_based)
            if end < start:
                raise ValueError("Invalid --page-range value: range end is before start.")
            pages.extend(range(start, end + 1))
        else:
            pages.append(parse_page_token(part, page_count, one_based=one_based))

    if not pages:
        raise ValueError("Invalid --page-range value: no pages selected.")
    return sorted(set(pages))
