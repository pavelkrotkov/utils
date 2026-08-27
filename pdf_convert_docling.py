#!/usr/bin/env python3
# /// script
# dependencies = ["docling", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using Docling.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_docling.py input.pdf

    # Standard execution:
    ./pdf_convert_docling.py input.pdf -o output.md
"""

from __future__ import annotations

import sys

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownText,
    Outcome,
    execute,
    require_module,
)


class DoclingBackend(Backend):
    name = "docling"
    description = "Convert a PDF to Markdown using Docling."

    def convert(self, request: ConversionRequest) -> Outcome:
        docling_converter = require_module("docling.document_converter", "docling")
        converter = docling_converter.DocumentConverter()

        try:
            if request.selection is None:
                result = converter.convert(str(request.pdf_path))
                if result.document is None:
                    raise ConversionError("Docling returned no document.")
                return MarkdownText(result.document.export_to_markdown())

            # Docling takes one inclusive (start, end) pair per call, so a
            # gapped selection becomes one call per run of pages.
            markdown_parts = []
            for page_range in request.selection.as_range_pairs():
                result = converter.convert(str(request.pdf_path), page_range=page_range)
                if result.document is None:
                    raise ConversionError("Docling returned no document.")
                markdown_parts.append(result.document.export_to_markdown().rstrip())
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"Docling conversion failed: {exc}") from exc

        return MarkdownText("\n\n".join(part for part in markdown_parts if part))


def main() -> None:
    sys.exit(execute(DoclingBackend()))


if __name__ == "__main__":
    main()
