#!/usr/bin/env python3
# /// script
# dependencies = ["docling", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using Docling.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_docling.py input.pdf

    # Standard execution:
    ./pdf_convert_docling.py input.pdf -o output.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using Docling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the input PDF file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output markdown file path (default: same name as PDF with .md)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write output files (defaults to input file directory)",
    )
    parser.add_argument(
        "--page-range",
        help=(
            "Comma-separated 1-based page numbers or ranges. "
            "Examples: 1-5, 1,3,5-10, 5-N (N = last page)."
        ),
    )
    return parser


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    resolved_dir = output_dir or input_path.parent
    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir / f"{input_path.stem}.md"


def load_pdf_page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Install with: pip install pypdf", file=sys.stderr)
        sys.exit(1)

    try:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as exc:
        print(f"ERROR: Unable to read PDF pages: {exc}", file=sys.stderr)
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


def collapse_page_ranges(pages: list[int]) -> list[tuple[int, int]]:
    if not pages:
        return []

    ranges: list[tuple[int, int]] = []
    start = pages[0]
    previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((start, previous))
        start = page
        previous = page
    ranges.append((start, previous))
    return ranges


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.pdf_path.exists() or not args.pdf_path.is_file():
        print(f"ERROR: PDF file not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Install with: pip install docling", file=sys.stderr)
        sys.exit(1)

    output_path = resolve_output_path(args.pdf_path, args.output, args.output_dir)

    page_ranges = None
    if args.page_range:
        try:
            page_count = load_pdf_page_count(args.pdf_path)
            pages = parse_page_range(args.page_range, page_count, one_based=True)
            page_ranges = collapse_page_ranges(pages)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    converter = DocumentConverter()
    try:
        if page_ranges is None:
            result = converter.convert(str(args.pdf_path))
        else:
            markdown_parts = []
            for page_range in page_ranges:
                result = converter.convert(str(args.pdf_path), page_range=page_range)
                if result.document is None:
                    print("ERROR: Docling returned no document.", file=sys.stderr)
                    sys.exit(1)
                markdown_parts.append(result.document.export_to_markdown().rstrip())
            markdown_text = "\n\n".join(part for part in markdown_parts if part)
            output_path.write_text(markdown_text, encoding="utf-8")
            print(f"Wrote Markdown to: {output_path}")
            return
    except Exception as exc:
        print(f"ERROR: Docling conversion failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.document is None:
        print("ERROR: Docling returned no document.", file=sys.stderr)
        sys.exit(1)

    markdown_text = result.document.export_to_markdown()
    output_path.write_text(markdown_text, encoding="utf-8")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
