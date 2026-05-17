#!/usr/bin/env python3
# /// script
# dependencies = ["mpxpy"]
# ///
"""
Convert a local PDF to Markdown (with LaTeX math) using Mathpix.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_mathpix_sdk.py input.pdf

    # Standard execution:
    ./pdf_convert_mathpix_sdk.py input.pdf -o output.md
"""

import argparse
import os
import sys
from pathlib import Path

from pdf_convert_common import import_or_die, require_pdf_path, resolve_output_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown with LaTeX formulas via Mathpix."
    )
    parser.add_argument("pdf_path", help="Path to the input PDF file")
    parser.add_argument(
        "-o", "--output", help="Output Markdown file path (default: same name as PDF, with .md)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for Mathpix processing (default: 300)",
    )
    args = parser.parse_args()

    pdf_path = require_pdf_path(args.pdf_path)

    app_id = os.environ.get("MATHPIX_APP_ID")
    app_key = os.environ.get("MATHPIX_APP_KEY") or os.environ.get("MATHPIX_API_KEY")

    if not app_id or not app_key:
        print(
            "Error: Please set MATHPIX_APP_ID and MATHPIX_APP_KEY (or MATHPIX_API_KEY) "
            "environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    mathpix_client = import_or_die("mpxpy.mathpix_client", "mpxpy")
    MathpixClient = mathpix_client.MathpixClient
    client = MathpixClient(app_id=app_id, app_key=app_key)

    pdf = client.pdf_new(
        file_path=str(pdf_path),
        convert_to_md=True,
        math_inline_delimiters=["$", "$"],
        math_display_delimiters=["$$", "$$"],
    )

    pdf.wait_until_complete(timeout=args.timeout)

    out_path = resolve_output_path(
        pdf_path,
        Path(args.output) if args.output else None,
        None,
    )

    pdf.to_md_file(path=str(out_path))

    print(f"Wrote Markdown to: {out_path}")


if __name__ == "__main__":
    main()
