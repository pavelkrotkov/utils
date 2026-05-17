#!/usr/bin/env python3
# /// script
# dependencies = ["marker-pdf", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using marker (best for simpler documents).

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_marker.py input.pdf

    # Standard execution:
    ./pdf_convert_marker.py input.pdf -o output.md
"""

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using marker.",
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
        "--force-ocr",
        action="store_true",
        help="Force OCR for all pages (slower, helps with poor PDFs)",
    )
    parser.add_argument(
        "--strip-existing-ocr",
        action="store_true",
        help="Remove embedded OCR text and re-OCR",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use an LLM to improve formatting accuracy (requires LLM config)",
    )
    parser.add_argument(
        "--llm-service",
        help="LLM service import path (e.g., marker.services.gemini.GoogleGeminiService)",
    )
    parser.add_argument(
        "--disable-image-extraction",
        action="store_true",
        help="Skip extracting images from the PDF",
    )
    parser.add_argument(
        "--config-json",
        type=Path,
        help="Path to marker config JSON for advanced settings",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output from marker",
    )
    return parser


def resolve_output_paths(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> tuple[Path, str]:
    if output_path is not None:
        resolved_dir = output_path.parent
        base_name = output_path.stem
    else:
        resolved_dir = output_dir or input_path.parent
        base_name = input_path.stem

    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir, base_name


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


def format_page_range(pages: list[int]) -> str:
    if not pages:
        return ""

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

    parts = [f"{begin}-{end}" if begin != end else str(begin) for begin, end in ranges]
    return ",".join(parts)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.pdf_path.exists() or not args.pdf_path.is_file():
        print(f"ERROR: PDF file not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    output_dir, base_name = resolve_output_paths(
        args.pdf_path,
        args.output,
        args.output_dir,
    )

    page_range = None
    if args.page_range:
        try:
            page_count = load_pdf_page_count(args.pdf_path)
            page_range = format_page_range(
                parse_page_range(args.page_range, page_count, one_based=False)
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        from marker.config.parser import ConfigParser
        from marker.models import create_model_dict
        from marker.output import save_output
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Install with: pip install marker-pdf", file=sys.stderr)
        sys.exit(1)

    config_options = {
        "output_format": "markdown",
        "output_dir": str(output_dir),
        "page_range": page_range,
        "force_ocr": args.force_ocr,
        "strip_existing_ocr": args.strip_existing_ocr,
        "use_llm": args.use_llm,
        "llm_service": args.llm_service,
        "disable_image_extraction": args.disable_image_extraction,
        "config_json": str(args.config_json) if args.config_json else None,
        "debug": args.debug,
    }

    config_parser = ConfigParser(config_options)
    config = config_parser.generate_config_dict()

    converter_cls = config_parser.get_converter_cls()
    renderer = config_parser.get_renderer()
    processor_list = config_parser.get_processors()
    llm_service = config_parser.get_llm_service()

    converter = converter_cls(
        artifact_dict=create_model_dict(),
        config=config,
        processor_list=processor_list,
        renderer=renderer,
        llm_service=llm_service,
    )

    rendered = converter(str(args.pdf_path))
    save_output(rendered, str(output_dir), base_name)

    output_md = output_dir / f"{base_name}.md"
    print(f"Wrote Markdown to: {output_md}")


if __name__ == "__main__":
    main()
