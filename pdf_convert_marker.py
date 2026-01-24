#!/usr/bin/env python3
# /// script
# dependencies = ["marker-pdf"]
# ///
"""
Convert a local PDF to Markdown using marker (best for simpler documents).

Usage:
    # Run with pipx (recommended):
    pipx run ./pdf_convert_marker.py input.pdf

    # Standard execution:
    ./pdf_convert_marker.py input.pdf -o output.md
"""

import argparse
import sys
from pathlib import Path

try:
    from marker.config.parser import ConfigParser
    from marker.models import create_model_dict
    from marker.output import save_output
except ImportError as e:
    print(f"ERROR: Missing required Python package: {e}", file=sys.stderr)
    print("Install with: pip install marker-pdf", file=sys.stderr)
    sys.exit(1)


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
        help="Comma-separated page numbers or ranges (e.g., 0,5-10,20)",
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

    config_options = {
        "output_format": "markdown",
        "output_dir": str(output_dir),
        "page_range": args.page_range,
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
