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

    # Override credentials and enable table fallback:
    ./pdf_convert_mathpix_sdk.py input.pdf --app-id YOUR_ID --app-key YOUR_KEY --enable-tables-fallback
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

from pdf_convert_common import import_or_die, require_pdf_path, resolve_output_path


def log(level: str, message: str) -> None:
    print(f"{level}: {message}", file=sys.stderr)


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    app_id = args.app_id or os.environ.get("MATHPIX_APP_ID")
    app_key = args.app_key or os.environ.get("MATHPIX_APP_KEY") or os.environ.get("MATHPIX_API_KEY")

    if not app_id or not app_key:
        raise ValueError(
            "Mathpix credentials not found. Pass --app-id/--app-key or set "
            "MATHPIX_APP_ID and MATHPIX_APP_KEY. MATHPIX_API_KEY may also be "
            "used as the app key."
        )

    return app_id, app_key


def main():
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown with LaTeX formulas via Mathpix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s document.pdf
  %(prog)s document.pdf -o notes.md
  %(prog)s document.pdf --app-id YOUR_ID --app-key YOUR_KEY
  %(prog)s document.pdf --no-rm-spaces --enable-tables-fallback

Environment Variables:
  MATHPIX_APP_ID     Your Mathpix application ID
  MATHPIX_APP_KEY    Your Mathpix application key
  MATHPIX_API_KEY    Alternative fallback for the application key
        """,
    )
    parser.add_argument("pdf_path", help="Path to the input PDF file")
    parser.add_argument(
        "-o", "--output", help="Output Markdown file path (default: same name as PDF, with .md)"
    )
    parser.add_argument("--app-id", help="Mathpix App ID (overrides MATHPIX_APP_ID)")
    parser.add_argument(
        "--app-key",
        help="Mathpix App Key (overrides MATHPIX_APP_KEY and MATHPIX_API_KEY)",
    )
    parser.add_argument(
        "--rm-spaces",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove extra whitespace from equations in Mathpix text outputs (default: enabled)",
    )
    parser.add_argument(
        "--enable-tables-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Mathpix's advanced fallback algorithm for large or complex tables (default: enabled)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for Mathpix processing (default: 300)",
    )
    parser.add_argument("--verbose", action="store_true", help="Show traceback details on errors")
    args = parser.parse_args()

    pdf_path = require_pdf_path(args.pdf_path)

    try:
        app_id, app_key = resolve_credentials(args)
        mathpix_client = import_or_die("mpxpy.mathpix_client", "mpxpy")
        client = mathpix_client.MathpixClient(app_id=app_id, app_key=app_key)

        log("INFO", f"Uploading {pdf_path} to Mathpix.")
        pdf = client.pdf_new(
            file_path=str(pdf_path),
            convert_to_md=True,
            math_inline_delimiters=["$", "$"],
            math_display_delimiters=["$$", "$$"],
            rm_spaces=args.rm_spaces,
            enable_tables_fallback=args.enable_tables_fallback,
        )

        log("INFO", f"Processing PDF {pdf.pdf_id}.")
        completed = pdf.wait_until_complete(timeout=args.timeout)
        if not completed:
            raise TimeoutError(
                f"Mathpix processing did not complete within {args.timeout} seconds."
            )

        out_path = resolve_output_path(
            pdf_path,
            Path(args.output) if args.output else None,
            None,
        )

        pdf.to_md_file(path=str(out_path))
        log("INFO", f"Wrote Markdown to {out_path}.")
    except Exception as exc:
        log("ERROR", str(exc))
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
