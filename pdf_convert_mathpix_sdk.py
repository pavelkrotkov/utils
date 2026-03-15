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

from mpxpy.mathpix_client import MathpixClient


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

    if not os.path.isfile(args.pdf_path):
        print(f"Error: '{args.pdf_path}' does not exist or is not a file.", file=sys.stderr)
        sys.exit(1)

    app_id = os.environ.get("MATHPIX_APP_ID")
    app_key = os.environ.get("MATHPIX_APP_KEY") or os.environ.get("MATHPIX_API_KEY")

    if not app_id or not app_key:
        print(
            "Error: Please set MATHPIX_APP_ID and MATHPIX_APP_KEY (or MATHPIX_API_KEY) "
            "environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = MathpixClient(app_id=app_id, app_key=app_key)

    pdf = client.pdf_new(
        file_path=args.pdf_path,
        convert_to_md=True,
        math_inline_delimiters=["$", "$"],
        math_display_delimiters=["$$", "$$"],
    )

    pdf.wait_until_complete(timeout=args.timeout)

    if args.output:
        out_path = args.output
    else:
        base, _ = os.path.splitext(args.pdf_path)
        out_path = base + ".md"

    pdf.to_md_file(path=out_path)

    print(f"Wrote Markdown to: {out_path}")


if __name__ == "__main__":
    main()
