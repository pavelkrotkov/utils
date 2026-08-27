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

from __future__ import annotations

import argparse
import os
import sys
import traceback

from pdf_convert_run import (
    AlreadyWritten,
    Backend,
    ConversionError,
    ConversionRequest,
    Outcome,
    execute,
    require_module,
)

EPILOG = """
Examples:
  %(prog)s document.pdf
  %(prog)s document.pdf -o notes.md
  %(prog)s document.pdf --app-id YOUR_ID --app-key YOUR_KEY
  %(prog)s document.pdf --no-rm-spaces --enable-tables-fallback

Environment Variables:
  MATHPIX_APP_ID     Your Mathpix application ID
  MATHPIX_APP_KEY    Your Mathpix application key
  MATHPIX_API_KEY    Alternative fallback for the application key
"""


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Return the Mathpix app id and key from flags or the environment."""
    app_id = args.app_id or os.environ.get("MATHPIX_APP_ID")
    app_key = args.app_key or os.environ.get("MATHPIX_APP_KEY") or os.environ.get("MATHPIX_API_KEY")

    if not app_id or not app_key:
        raise ConversionError(
            "Mathpix credentials not found. Pass --app-id/--app-key or set "
            "MATHPIX_APP_ID and MATHPIX_APP_KEY. MATHPIX_API_KEY may also be "
            "used as the app key."
        )

    return app_id, app_key


class MathpixBackend(Backend):
    name = "mathpix"
    description = "Convert a PDF to Markdown with LaTeX formulas via Mathpix."
    epilog = EPILOG
    # The Mathpix SDK converts whole documents; it exposes no page argument.
    supports_page_selection = False

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
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
        parser.add_argument(
            "--verbose", action="store_true", help="Show traceback details on errors"
        )

    def validate(self, args: argparse.Namespace) -> None:
        if args.timeout < 1:
            raise ConversionError("--timeout must be at least 1 second.")
        resolve_credentials(args)

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        app_id, app_key = resolve_credentials(args)
        mathpix_client = require_module("mpxpy.mathpix_client", "mpxpy")

        try:
            client = mathpix_client.MathpixClient(app_id=app_id, app_key=app_key)

            print(f"INFO: Uploading {request.pdf_path} to Mathpix.", file=sys.stderr)
            pdf = client.pdf_new(
                file_path=str(request.pdf_path),
                convert_to_md=True,
                math_inline_delimiters=["$", "$"],
                math_display_delimiters=["$$", "$$"],
                rm_spaces=args.rm_spaces,
                enable_tables_fallback=args.enable_tables_fallback,
            )

            print(f"INFO: Processing PDF {pdf.pdf_id}.", file=sys.stderr)
            if not pdf.wait_until_complete(timeout=args.timeout):
                raise ConversionError(
                    f"Mathpix processing did not complete within {args.timeout} seconds."
                )

            pdf.to_md_file(path=str(request.output_path))
        except ConversionError:
            raise
        except Exception as exc:
            if args.verbose:
                traceback.print_exc()
            raise ConversionError(str(exc)) from exc

        return AlreadyWritten()


def main() -> None:
    sys.exit(execute(MathpixBackend()))


if __name__ == "__main__":
    main()
