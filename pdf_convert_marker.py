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

from pdf_convert_common import (
    collapse_consecutive,
    format_page_ranges,
    import_or_die,
    parse_page_range,
    require_pdf_path,
    resolve_output_path,
)


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

    output_path = resolve_output_path(
        pdf_path,
        args.output,
        args.output_dir,
    )
    output_dir = output_path.parent
    base_name = output_path.stem

    page_range = None
    if args.page_range:
        try:
            page_count = load_pdf_page_count(pdf_path)
            pages = parse_page_range(args.page_range, page_count, one_based=False)
            page_range = format_page_ranges(collapse_consecutive(pages))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    marker_parser = import_or_die("marker.config.parser", "marker-pdf")
    marker_models = import_or_die("marker.models", "marker-pdf")
    marker_output = import_or_die("marker.output", "marker-pdf")
    ConfigParser = marker_parser.ConfigParser
    create_model_dict = marker_models.create_model_dict
    save_output = marker_output.save_output

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

    rendered = converter(str(pdf_path))
    save_output(rendered, str(output_dir), base_name)

    output_md = output_dir / f"{base_name}.md"
    print(f"Wrote Markdown to: {output_md}")


if __name__ == "__main__":
    main()
