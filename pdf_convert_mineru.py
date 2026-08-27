#!/usr/bin/env python3
# /// script
# dependencies = ["mineru[all]", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using MinerU.

Models are downloaded automatically on first run. The CPU-friendly
``pipeline`` backend is used by default; pass ``-b vlm-engine`` (MinerU 2.5
Pro VLM) or ``-b hybrid-engine`` for higher accuracy where hardware allows.
Thread pools are forced to --threads to keep CPU and memory usage bounded.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_mineru.py input.pdf
    uv run ./pdf_convert_mineru.py input.pdf --page-range 1-5

    # Standard execution:
    ./pdf_convert_mineru.py input.pdf -o output.md

    # VLM backend (higher accuracy, needs beefier hardware):
    uv run ./pdf_convert_mineru.py input.pdf -b vlm-engine
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pdf_convert_common import (
    copy_referenced_assets,
    find_generated_markdown,
    require_pdf_path,
    resolve_output_path,
)
from pdf_page_selection import (
    PAGE_RANGE_HELP,
    PageSelection,
    PageSelectionError,
    load_page_count,
)

BACKEND_CHOICES = (
    "pipeline",
    "vlm-engine",
    "hybrid-engine",
    "vlm-http-client",
    "hybrid-http-client",
)
METHOD_CHOICES = ("auto", "txt", "ocr")
DEFAULT_THREADS = 4
THREAD_LIMIT_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def apply_thread_limit(threads: int) -> None:
    """Force numeric thread pools before spawning the MinerU process."""
    for env_var in THREAD_LIMIT_ENV_VARS:
        os.environ[env_var] = str(threads)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using MinerU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument(
        "--page-range",
        help=f"{PAGE_RANGE_HELP} MinerU supports contiguous ranges only.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=BACKEND_CHOICES,
        default="pipeline",
        help=(
            f"Parsing backend (default: {BACKEND_CHOICES[0]} for CPU safety). "
            f"MinerU's own default is {BACKEND_CHOICES[2]}; "
            "the *-http-client backends also need --backend-url."
        ),
    )
    parser.add_argument(
        "-m",
        "--method",
        choices=METHOD_CHOICES,
        help="Parsing method for pipeline-style backends (default: auto)",
    )
    parser.add_argument(
        "-u",
        "--backend-url",
        help="OpenAI-compatible server URL for *-http-client backends",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=(
            f"Thread pool count forced onto OMP/BLAS libraries (default: {DEFAULT_THREADS}); "
            "overrides ambient thread environment variables"
        ),
    )
    return parser


def locate_mineru_binary() -> str:
    candidate = shutil.which("mineru")
    if candidate is not None:
        return candidate

    local_candidate = Path(sys.executable).with_name("mineru")
    if local_candidate.is_file():
        return str(local_candidate)

    print(
        "ERROR: The 'mineru' executable was not found in this environment.",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_contiguous_pages(spec: str, pdf_path: Path) -> tuple[int, int]:
    """Resolve a page-range spec to the single run of pages MinerU accepts."""
    try:
        selection = PageSelection.parse(spec, load_page_count(pdf_path))
    except PageSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        return selection.as_contiguous()
    except PageSelectionError as exc:
        print(
            f"ERROR: MinerU supports contiguous page ranges only; {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.threads < 1:
        print("ERROR: --threads must be at least 1.", file=sys.stderr)
        sys.exit(1)
    apply_thread_limit(args.threads)

    pdf_path = require_pdf_path(args.pdf_path)
    mineru_bin = locate_mineru_binary()

    output_path = resolve_output_path(pdf_path, args.output, args.output_dir)

    command = [mineru_bin, "-p", str(pdf_path)]
    with tempfile.TemporaryDirectory(prefix="mineru_") as work_dir_raw:
        work_dir = Path(work_dir_raw)
        command.extend(["-o", str(work_dir)])

        if args.page_range is not None:
            first_page, last_page = resolve_contiguous_pages(args.page_range, pdf_path)
            command.extend(["-s", str(first_page - 1), "-e", str(last_page - 1)])
        if args.backend is not None:
            command.extend(["-b", args.backend])
        if args.method is not None:
            command.extend(["-m", args.method])
        if args.backend_url is not None:
            command.extend(["-u", args.backend_url])

        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            print(
                f"ERROR: MinerU exited with status {exc.returncode}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(f"ERROR: Failed to launch MinerU: {exc}", file=sys.stderr)
            sys.exit(1)

        generated_md = find_generated_markdown(work_dir)
        if generated_md is None:
            print(
                f"ERROR: MinerU did not produce a Markdown file in: {work_dir}",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            generated_md.resolve().relative_to(work_dir.resolve())
        except ValueError:
            print(
                f"ERROR: Generated file is outside temporary directory: {generated_md}",
                file=sys.stderr,
            )
            sys.exit(1)

        markdown_text = generated_md.read_text(encoding="utf-8")
        copied_assets = copy_referenced_assets(
            markdown_text,
            generated_md.parent,
            output_path.parent,
        )

    try:
        output_path.write_text(markdown_text, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Unable to write Markdown output: {exc}", file=sys.stderr)
        sys.exit(1)
    for asset_path in copied_assets:
        print(f"INFO: Copied referenced asset: {asset_path}")
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()
