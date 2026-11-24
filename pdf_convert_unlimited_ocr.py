#!/usr/bin/env python3
# /// script
# dependencies = ["pymupdf==1.27.2.2", "pypdf"]
# ///
"""Convert selected PDF pages together through an Unlimited-OCR vLLM server.

Start the upstream GPU server separately; this client downloads no model weights.
    uv run ./pdf_convert_unlimited_ocr.py input.pdf --page-range 1-3,5 -o output.md
    uv run ./pdf_convert_unlimited_ocr.py input.pdf --base-url http://gpu:8000/v1
"""

from __future__ import annotations

import argparse
import base64
import math
import sys
from urllib.parse import urlsplit

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownText,
    execute,
    require_module,
)
from pdf_unlimited_ocr_api import completion


def api_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConversionError(f"Invalid --base-url: {exc}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or port == 0:
        raise ConversionError(
            "--base-url must use HTTP(S) with a hostname and valid optional port."
        )
    if any(("?" in value, "#" in value, parsed.username is not None)):
        raise ConversionError("--base-url must not contain credentials, a query or a fragment.")
    return value.rstrip("/") + "/chat/completions"


def page_images(request: ConversionRequest) -> list[dict[str, object]]:
    pymupdf = require_module("pymupdf", "pymupdf")
    try:
        with pymupdf.open(request.pdf_path) as document:
            pages = request.selection.as_zero_based() if request.selection else range(len(document))
            images: list[dict[str, object]] = []
            for page in pages:
                png = document[page].get_pixmap(dpi=request.args.dpi, alpha=False).tobytes("png")
                encoded = base64.b64encode(png).decode("ascii")
                images.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
                )
            return images
    except Exception as exc:
        raise ConversionError(f"Unable to render PDF pages: {exc}") from exc


class UnlimitedOCRBackend(Backend):
    name = "unlimited_ocr"
    description = "Convert a PDF to Markdown using an Unlimited-OCR vLLM server."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--base-url",
            default="http://127.0.0.1:8000/v1",
            help="vLLM API URL including /v1; PDF page images are sent here",
        )
        parser.add_argument(
            "--model",
            default="baidu/Unlimited-OCR",
            help="Unlimited-OCR v1 served model name or alias",
        )
        parser.add_argument(
            "--timeout", type=float, default=600, help="HTTP timeout in seconds (default: 600)"
        )
        parser.add_argument(
            "--max-tokens",
            type=int,
            default=8192,
            help="Output token limit (default: 8192); truncated output is rejected",
        )
        parser.add_argument(
            "--dpi", type=int, default=150, help="PDF rasterization resolution (default: 150 DPI)"
        )

    def validate(self, args: argparse.Namespace) -> None:
        args.endpoint = api_endpoint(args.base_url)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ConversionError("--timeout must be finite and positive.")
        if min(args.dpi, args.max_tokens) <= 0:
            raise ConversionError("--dpi and --max-tokens must be positive.")

    def convert(self, request: ConversionRequest) -> MarkdownText:
        return MarkdownText(completion(request.args, page_images(request)))


def main() -> None:
    sys.exit(execute(UnlimitedOCRBackend()))


if __name__ == "__main__":
    main()
