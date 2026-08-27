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

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownText,
    Outcome,
    execute,
    require_module,
)
from pdf_page_selection import PageSelection, PageSelectionError

DEFAULT_DPI = 150
DEFAULT_IMAGE_FORMAT = "png"


class PyMuPdf4LlmBackend(Backend):
    name = "pymupdf4llm"
    description = "Convert a PDF to Markdown using pymupdf4llm."
    # PyMuPDF is already a dependency here, so the page count comes from the
    # open document rather than pulling in a second PDF library. The run module
    # never needs to resolve a selection for this backend.
    supports_page_selection = False

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--page-range", help=self.page_range_help())
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

    def validate(self, args: argparse.Namespace) -> None:
        if args.write_images and args.embed_images:
            raise ConversionError("--write-images and --embed-images cannot be used together.")

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        pymupdf = require_module("pymupdf", "pymupdf4llm")
        if args.layout:
            require_module("pymupdf.layout", "pymupdf4llm[layout]")
        pymupdf4llm = require_module("pymupdf4llm", "pymupdf4llm")

        pages = None
        if args.page_range:
            pages = self._resolve_pages(pymupdf, request, args.page_range)

        image_path = ""
        if args.write_images:
            image_dir = args.images_dir or request.output_path.parent
            try:
                image_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConversionError(f"Unable to create the image directory: {exc}") from exc
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
            markdown_text = pymupdf4llm.to_markdown(str(request.pdf_path), **kwargs)
        except Exception as exc:
            raise ConversionError(f"pymupdf4llm failed: {exc}") from exc
        return MarkdownText(markdown_text)

    @staticmethod
    def _resolve_pages(pymupdf, request: ConversionRequest, spec: str) -> list[int]:
        try:
            doc = pymupdf.open(str(request.pdf_path))
        except Exception as exc:
            raise ConversionError(f"Unable to open PDF: {exc}") from exc
        try:
            return PageSelection.parse(spec, doc.page_count).as_zero_based()
        except PageSelectionError as exc:
            raise ConversionError(str(exc)) from exc
        finally:
            doc.close()


def main() -> None:
    sys.exit(execute(PyMuPdf4LlmBackend()))


if __name__ == "__main__":
    main()
