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
    import_or_die,
    require_pdf_path,
    resolve_output_path,
)
from pdf_page_selection import (
    PAGE_RANGE_HELP,
    PageSelection,
    PageSelectionError,
    load_page_count,
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
    parser.add_argument("--page-range", help=PAGE_RANGE_HELP)
    return parser


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
            selection = PageSelection.parse(args.page_range, load_page_count(pdf_path))
        except PageSelectionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        page_ranges = selection.as_range_pairs()

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
