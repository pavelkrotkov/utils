#!/usr/bin/env python3
# /// script
# dependencies = ["docling"]
# ///
"""
Convert a local PDF to Markdown using Docling.

Usage:
    # Run with pipx (recommended):
    pipx run ./pdf_convert_docling.py input.pdf

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
            "Page range to convert (1-based, contiguous). Examples: 1-5, 3-10, 2-N (N = last page)."
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


def parse_page_token(token: str) -> int:
    if token.upper() == "N":
        return sys.maxsize
    try:
        value = int(token)
    except ValueError as exc:
        raise ValueError("Invalid --page-range value.") from exc
    if value < 1:
        raise ValueError("--page-range must be 1-based.")
    return value


def parse_page_range(page_range: str) -> tuple[int, int]:
    if "," in page_range:
        raise ValueError("Docling only supports contiguous page ranges.")

    raw = page_range.strip()
    if not raw:
        raise ValueError("Invalid --page-range value.")

    if "-" in raw:
        start_raw, end_raw = raw.split("-", 1)
        if not start_raw or not end_raw:
            raise ValueError("Invalid --page-range value.")
        start = parse_page_token(start_raw.strip())
        end = parse_page_token(end_raw.strip())
    else:
        if raw.upper() == "N":
            raise ValueError("Use START-N to indicate the last page.")
        start = parse_page_token(raw)
        end = start

    if end < start:
        raise ValueError("Invalid --page-range value.")

    return start, end


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

    page_range = None
    if args.page_range:
        try:
            page_range = parse_page_range(args.page_range)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    converter = DocumentConverter()
    try:
        if page_range is None:
            result = converter.convert(str(args.pdf_path))
        else:
            result = converter.convert(str(args.pdf_path), page_range=page_range)
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
