#!/usr/bin/env python3
# /// script
# dependencies = ["opendataloader-pdf", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using OpenDataLoader PDF.

Requires Java 11+ available on PATH (e.g. `brew install --cask temurin`).

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_opendataloader.py input.pdf

    # Standard execution:
    ./pdf_convert_opendataloader.py input.pdf -o output.md
"""

from __future__ import annotations

import argparse
import shutil
import sys

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownDirectory,
    Outcome,
    execute,
    require_module,
)

IMAGE_OUTPUT_CHOICES = ("external", "embedded", "off")
TABLE_METHOD_CHOICES = ("default", "cluster")


class OpenDataLoaderBackend(Backend):
    name = "opendataloader"
    description = "Convert a PDF to Markdown using OpenDataLoader PDF."
    epilog = (
        "Environment:\n  Requires Java 11+ on PATH (install with: brew install --cask temurin).\n"
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--password",
            help="Password for encrypted PDF files",
        )
        parser.add_argument(
            "--keep-line-breaks",
            action="store_true",
            help="Preserve original line breaks in extracted text",
        )
        parser.add_argument(
            "--use-struct-tree",
            action="store_true",
            help=(
                "Use the PDF structure tree for reading order and semantics "
                "(best for well-tagged PDFs)"
            ),
        )
        parser.add_argument(
            "--table-method",
            choices=TABLE_METHOD_CHOICES,
            default="default",
            help=f"Table detection method (default: {TABLE_METHOD_CHOICES[0]})",
        )
        parser.add_argument(
            "--include-header-footer",
            action="store_true",
            help="Include page headers and footers in output",
        )
        parser.add_argument(
            "--image-output",
            choices=IMAGE_OUTPUT_CHOICES,
            default="external",
            help=(
                "Image output mode: external (file references), embedded (base64), "
                f"or off (default: {IMAGE_OUTPUT_CHOICES[0]})"
            ),
        )
        parser.add_argument(
            "--markdown-with-html",
            action="store_true",
            help=(
                "Allow HTML tags inside Markdown for complex structures such as "
                "multi-row-span tables"
            ),
        )

    def validate(self, args: argparse.Namespace) -> None:
        if shutil.which("java") is None:
            raise ConversionError(
                "Java 11+ is required by OpenDataLoader PDF but was not found on PATH. "
                "Install with: brew install --cask temurin"
            )

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        opendataloader = require_module("opendataloader_pdf", "opendataloader-pdf")

        convert_kwargs: dict[str, object] = {
            "input_path": [str(request.pdf_path)],
            "output_dir": str(request.workspace),
            "format": "markdown",
        }
        if request.selection is not None:
            convert_kwargs["pages"] = request.selection.as_ranges()
        if args.password is not None:
            convert_kwargs["password"] = args.password
        if args.keep_line_breaks:
            convert_kwargs["keep_line_breaks"] = True
        if args.use_struct_tree:
            convert_kwargs["use_struct_tree"] = True
        if args.table_method != TABLE_METHOD_CHOICES[0]:
            convert_kwargs["table_method"] = args.table_method
        if args.include_header_footer:
            convert_kwargs["include_header_footer"] = True
        if args.image_output != IMAGE_OUTPUT_CHOICES[0]:
            convert_kwargs["image_output"] = args.image_output
        if args.markdown_with_html:
            convert_kwargs["markdown_with_html"] = True

        try:
            opendataloader.convert(**convert_kwargs)  # type: ignore[call-arg]
        except Exception as exc:
            raise ConversionError(f"OpenDataLoader conversion failed: {exc}") from exc

        return MarkdownDirectory(request.workspace, expected_stem=request.pdf_path.stem)


def main() -> None:
    sys.exit(execute(OpenDataLoaderBackend()))


if __name__ == "__main__":
    main()
