#!/usr/bin/env python3
"""Shared helpers for PDF conversion scripts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    """Resolve a Markdown output path and ensure its parent directory exists."""
    if output_path is not None:
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
