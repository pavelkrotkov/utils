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
import tempfile
from pathlib import Path

from pdf_convert_common import (
    copy_referenced_assets,
    import_or_die,
    require_pdf_path,
    resolve_output_path,
)
from pdf_page_selection import (
    PAGE_RANGE_HELP,
    PageSelection,
    PageSelectionError,
    load_page_count,
)

IMAGE_OUTPUT_CHOICES = ("external", "embedded", "off")
TABLE_METHOD_CHOICES = ("default", "cluster")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using OpenDataLoader PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment:\n"
            "  Requires Java 11+ on PATH "
            "(install with: brew install --cask temurin).\n"
        ),
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the input PDF file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output markdown file path (default: same name as PDF with .md)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write output files (defaults to input file directory)",
    )
    parser.add_argument("--page-range", help=PAGE_RANGE_HELP)
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
        help="Use the PDF structure tree for reading order and semantics (best for well-tagged PDFs)",
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
        help="Allow HTML tags inside Markdown for complex structures such as multi-row-span tables",
    )
    return parser


def require_java() -> None:
    if shutil.which("java") is not None:
        return
    print(
        "ERROR: Java 11+ is required by OpenDataLoader PDF but was not found on PATH.",
        file=sys.stderr,
    )
    print("Install with: brew install --cask temurin", file=sys.stderr)
    sys.exit(1)


def locate_generated_markdown(work_dir: Path, expected_stem: str) -> Path | None:
    expected_path = work_dir / f"{expected_stem}.md"
    if expected_path.is_file():
        return expected_path

    candidates = sorted(path for path in work_dir.rglob("*.md") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    require_java()

    pdf_path = require_pdf_path(args.pdf_path)

    opendataloader = import_or_die("opendataloader_pdf", "opendataloader-pdf")

    output_path = resolve_output_path(pdf_path, args.output, args.output_dir)

    pages_spec = None
    if args.page_range:
        try:
            selection = PageSelection.parse(args.page_range, load_page_count(pdf_path))
        except PageSelectionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        pages_spec = selection.as_ranges()

    convert_kwargs: dict[str, object] = {
        "input_path": [str(pdf_path)],
        "output_dir": "",
        "format": "markdown",
    }
    if pages_spec is not None:
        convert_kwargs["pages"] = pages_spec
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

    with tempfile.TemporaryDirectory(prefix="opendataloader_") as work_dir_raw:
        work_dir = Path(work_dir_raw)
        convert_kwargs["output_dir"] = str(work_dir)

        try:
            opendataloader.convert(**convert_kwargs)  # type: ignore[call-arg]
        except Exception as exc:
            print(f"ERROR: OpenDataLoader conversion failed: {exc}", file=sys.stderr)
            sys.exit(1)

        generated_md = locate_generated_markdown(work_dir, pdf_path.stem)
        if generated_md is None:
            print(
                f"ERROR: OpenDataLoader did not produce a Markdown file in: {work_dir}",
                file=sys.stderr,
            )
            sys.exit(1)

        markdown_text = generated_md.read_text(encoding="utf-8")

        copied_assets = copy_referenced_assets(
            markdown_text,
            generated_md.parent,
            output_path.parent,
        )

    output_path.write_text(markdown_text, encoding="utf-8")
    for asset_path in copied_assets:
        print(f"INFO: Copied referenced asset: {asset_path}")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
