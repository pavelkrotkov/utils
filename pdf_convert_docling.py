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

from pdf_convert_common import (
    collapse_consecutive,
    import_or_die,
    parse_page_range,
    require_pdf_path,
    resolve_output_path,
)


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


def load_pdf_page_count(pdf_path: Path) -> int:
    pypdf = import_or_die("pypdf", "pypdf")

    try:
        reader = pypdf.PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as exc:
        print(f"ERROR: Unable to read PDF pages: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    pdf_path = require_pdf_path(args.pdf_path)

    docling_converter = import_or_die("docling.document_converter", "docling")
    DocumentConverter = docling_converter.DocumentConverter

    output_path = resolve_output_path(pdf_path, args.output, args.output_dir)

    page_ranges = None
    if args.page_range:
        try:
            page_count = load_pdf_page_count(pdf_path)
            pages = parse_page_range(args.page_range, page_count, one_based=True)
            page_ranges = collapse_consecutive(pages)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    converter = DocumentConverter()
    try:
        if page_ranges is None:
            result = converter.convert(str(pdf_path))
        else:
            markdown_parts = []
            for page_range in page_ranges:
                result = converter.convert(str(pdf_path), page_range=page_range)
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
