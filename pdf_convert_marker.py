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

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pdf_convert_run import (
    AlreadyWritten,
    Backend,
    ConversionError,
    ConversionRequest,
    Outcome,
    execute,
    require_module,
)


class MarkerBackend(Backend):
    name = "marker"
    description = "Convert a PDF to Markdown using marker."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
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

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        output_dir = request.output_path.parent
        base_name = request.output_path.stem

        # marker indexes pages from 0 and takes a comma-separated spec string.
        page_range = None
        if request.selection is not None:
            page_range = request.selection.as_ranges(zero_based=True)

        marker_parser = require_module("marker.config.parser", "marker-pdf")
        marker_models = require_module("marker.models", "marker-pdf")
        marker_output = require_module("marker.output", "marker-pdf")

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

        try:
            config_parser = marker_parser.ConfigParser(config_options)
            converter_cls = config_parser.get_converter_cls()
            converter = converter_cls(
                artifact_dict=marker_models.create_model_dict(),
                config=config_parser.generate_config_dict(),
                processor_list=config_parser.get_processors(),
                renderer=config_parser.get_renderer(),
                llm_service=config_parser.get_llm_service(),
            )
            rendered = converter(str(request.pdf_path))
            # marker names its own output: <output_dir>/<base_name>.md, whatever
            # suffix -o asked for. Report the path it actually wrote.
            marker_output.save_output(rendered, str(output_dir), base_name)
        except Exception as exc:
            raise ConversionError(f"marker conversion failed: {exc}") from exc

        return AlreadyWritten(output_dir / f"{base_name}.md")


def main() -> None:
    sys.exit(execute(MarkerBackend()))


if __name__ == "__main__":
    main()
