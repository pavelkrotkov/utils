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
            "Comma-separated page numbers or ranges (0-based, e.g., 0,5-10,20). "
            "Use N for the last page."
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


def parse_page_token(token: str, last_page: int) -> int:
    if token.lower() == "n":
        return last_page
    try:
        return int(token)
    except ValueError as exc:
        raise ValueError("Invalid --page-range value.") from exc


def parse_page_range(page_range: str, page_count: int) -> list[int]:
    if page_count < 1:
        raise ValueError("PDF has no pages.")

    last_page = page_count - 1
    pages: list[int] = []
    for raw_part in page_range.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            start = parse_page_token(start_raw.strip(), last_page)
            end = parse_page_token(end_raw.strip(), last_page)
            if start < 0 or end < 0 or start > end:
                raise ValueError("Invalid --page-range value.")
            pages.extend(range(start, end + 1))
        else:
            page = parse_page_token(part, last_page)
            if page < 0:
                raise ValueError("Invalid --page-range value.")
            pages.append(page)

    if not pages:
        raise ValueError("--page-range produced no pages.")

    invalid = [page for page in pages if page > last_page]
    if invalid:
        raise ValueError("--page-range includes pages outside the document.")

    return sorted(set(pages))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.pdf_path.exists() or not args.pdf_path.is_file():
        print(f"ERROR: PDF file not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    if args.write_images and args.embed_images:
        print(
            "ERROR: --write-images and --embed-images cannot be used together.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import pymupdf
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Install with: pip install pymupdf4llm", file=sys.stderr)
        sys.exit(1)

    if args.layout:
        try:
            import pymupdf.layout  # noqa: F401
        except ImportError as exc:
            print(f"ERROR: Layout mode requires pymupdf4llm[layout]: {exc}", file=sys.stderr)
            print("Install with: pip install pymupdf4llm[layout]", file=sys.stderr)
            sys.exit(1)

    try:
        import pymupdf4llm
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Install with: pip install pymupdf4llm", file=sys.stderr)
        sys.exit(1)

    output_path = resolve_output_path(args.pdf_path, args.output, args.output_dir)

    pages = None
    if args.page_range:
        try:
            doc = pymupdf.open(str(args.pdf_path))
        except Exception as exc:
            print(f"ERROR: Unable to open PDF: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            pages = parse_page_range(args.page_range, doc.page_count)
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
        markdown_text = pymupdf4llm.to_markdown(str(args.pdf_path), **kwargs)
    except Exception as exc:
        print(f"ERROR: pymupdf4llm failed: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path.write_text(markdown_text, encoding="utf-8")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
