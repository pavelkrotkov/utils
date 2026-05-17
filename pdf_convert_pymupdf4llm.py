#!/usr/bin/env python3
# /// script
# dependencies = ["pymupdf4llm[layout]"]
# ///
"""
Convert a local PDF to Markdown using pymupdf4llm (PyMuPDF).

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_pymupdf4llm.py input.pdf

    # Standard execution:
    ./pdf_convert_pymupdf4llm.py input.pdf -o output.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pdf_convert_common import (
    import_or_die,
    parse_page_range,
    require_pdf_path,
    resolve_output_path,
)


DEFAULT_DPI = 150
DEFAULT_IMAGE_FORMAT = "png"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using pymupdf4llm.",
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
    parser.add_argument(
        "--layout",
        action="store_true",
        help="Enable layout mode (requires pymupdf4llm[layout])",
    )
    parser.add_argument(
        "--write-images",
        action="store_true",
        help="Write extracted images to disk",
    )
    parser.add_argument(
        "--embed-images",
        action="store_true",
        help="Embed extracted images as base64 in markdown",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        help="Directory to write extracted images (defaults to output directory)",
    )
    parser.add_argument(
        "--image-format",
        default=DEFAULT_IMAGE_FORMAT,
        help=f"Image format for extracted images (default: {DEFAULT_IMAGE_FORMAT})",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"DPI for rendered images (default: {DEFAULT_DPI})",
    )
    force_group = parser.add_mutually_exclusive_group()
    force_group.add_argument(
        "--force-text",
        action="store_true",
        help="Force text extraction in image areas",
    )
    force_group.add_argument(
        "--no-force-text",
        action="store_true",
        help="Suppress text extraction in image areas",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    pdf_path = require_pdf_path(args.pdf_path)

    if args.write_images and args.embed_images:
        print(
            "ERROR: --write-images and --embed-images cannot be used together.",
            file=sys.stderr,
        )
        sys.exit(1)

    pymupdf = import_or_die("pymupdf", "pymupdf4llm")

    if args.layout:
        import_or_die("pymupdf.layout", "pymupdf4llm[layout]")

    pymupdf4llm = import_or_die("pymupdf4llm", "pymupdf4llm")

    output_path = resolve_output_path(pdf_path, args.output, args.output_dir)

    pages = None
    if args.page_range:
        try:
            doc = pymupdf.open(str(pdf_path))
        except Exception as exc:
            print(f"ERROR: Unable to open PDF: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            pages = parse_page_range(args.page_range, doc.page_count, one_based=False)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        finally:
            doc.close()

    image_path = ""
    if args.write_images:
        image_dir = args.images_dir or output_path.parent
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = str(image_dir)

    kwargs = {
        "pages": pages,
        "write_images": args.write_images,
        "embed_images": args.embed_images,
        "image_path": image_path,
        "image_format": args.image_format,
        "dpi": args.dpi,
    }
    if args.force_text:
        kwargs["force_text"] = True
    elif args.no_force_text:
        kwargs["force_text"] = False

    try:
        markdown_text = pymupdf4llm.to_markdown(str(pdf_path), **kwargs)
    except Exception as exc:
        print(f"ERROR: pymupdf4llm failed: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path.write_text(markdown_text, encoding="utf-8")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
