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
        markdown_text = pymupdf4llm.to_markdown(str(args.pdf_path), **kwargs)
    except Exception as exc:
        print(f"ERROR: pymupdf4llm failed: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path.write_text(markdown_text, encoding="utf-8")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
