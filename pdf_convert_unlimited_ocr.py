#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pymupdf==1.27.2.2",
#   "pypdf",
#   "mlx-vlm==0.8.1; platform_system == 'Darwin' and platform_machine == 'arm64'",
# ]
# ///
"""Convert PDF pages to Markdown with Unlimited-OCR v1 on Apple Silicon.

uv run ./pdf_convert_unlimited_ocr.py input.pdf -o output.md
uv run ./pdf_convert_unlimited_ocr.py input.pdf --page-range 1-3,5
"""

from __future__ import annotations

import argparse
import platform
import sys

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownText,
    execute,
    require_module,
)
from pdf_unlimited_ocr_mlx import load_ocr

DEFAULT_MODEL = "mlx-community/Unlimited-OCR-4bit"


class UnlimitedOCRBackend(Backend):
    name = "unlimited_ocr"
    description = "Convert a PDF to Markdown locally with Unlimited-OCR and MLX."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--model", default=DEFAULT_MODEL, help="Unlimited-OCR v1 MLX model ID or local path"
        )
        parser.add_argument(
            "--max-tokens",
            type=int,
            default=8192,
            help="Output token limit per page (default: 8192)",
        )
        parser.add_argument(
            "--dpi", type=int, default=150, help="PDF rasterization resolution (default: 150 DPI)"
        )

    def validate(self, args: argparse.Namespace) -> None:
        if min(args.dpi, args.max_tokens) <= 0:
            raise ConversionError("--dpi and --max-tokens must be positive.")
        if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
            raise ConversionError("Unlimited-OCR requires native macOS Python on Apple Silicon.")

    def convert(self, request: ConversionRequest) -> MarkdownText:
        pymupdf = require_module("pymupdf", "pymupdf")
        try:
            document = pymupdf.open(request.pdf_path)
        except Exception as exc:
            raise ConversionError(f"Unable to open PDF: {exc}") from exc
        with document:
            pages = (
                request.selection.as_one_based()
                if request.selection
                else range(1, len(document) + 1)
            )
            if not pages:
                raise ConversionError("PDF has no pages to parse.")
            parse = load_ocr(request.args.model, request.args.max_tokens)
            image_path = request.workspace / "page.png"
            markdown = []
            for page in pages:
                print(f"INFO: Parsing page {page} with Unlimited-OCR", file=sys.stderr)
                try:
                    document[page - 1].get_pixmap(dpi=request.args.dpi, alpha=False).save(
                        image_path
                    )
                    markdown.append(parse(image_path))
                except Exception as exc:
                    raise ConversionError(f"Unlimited-OCR failed on page {page}: {exc}") from exc
        return MarkdownText("\n\n---\n\n".join(markdown) + "\n")


def main() -> None:
    sys.exit(execute(UnlimitedOCRBackend()))


if __name__ == "__main__":
    main()
