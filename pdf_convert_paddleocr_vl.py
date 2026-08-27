#!/usr/bin/env python3
# /// script
# dependencies = ["paddlepaddle>=3.2", "paddleocr[doc-parser]>=3.3", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using PaddleOCR-VL.

The first run downloads the layout and VLM models automatically.
Thread pools (OMP/MKL/OpenBLAS/NumExpr/Paddle) are forced to --threads
to keep CPU and memory usage bounded on small machines.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_paddleocr_vl.py input.pdf
    uv run ./pdf_convert_paddleocr_vl.py input.pdf --page-range 1-5

    # Standard execution:
    ./pdf_convert_paddleocr_vl.py input.pdf -o output.md
    ./pdf_convert_paddleocr_vl.py input.pdf --threads 4 --device cpu
"""

from __future__ import annotations

import argparse
import os
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

ENGINE_CHOICES = ("paddle", "transformers")
PIPELINE_VERSION_CHOICES = ("v1", "v1.5", "v1.6")
DEFAULT_THREADS = 4
# Thread pool sizes read by OpenMP, BLAS libraries, NumExpr, PaddlePaddle,
# and Accelerate (macOS).
THREAD_LIMIT_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "CPU_NUM",
    "VECLIB_MAXIMUM_THREADS",
)


def apply_thread_limit(threads: int) -> None:
    """Force numeric thread pools before the framework libraries load."""
    for env_var in THREAD_LIMIT_ENV_VARS:
        os.environ[env_var] = str(threads)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using PaddleOCR-VL.",
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
    parser.add_argument("--page-range", help=PAGE_RANGE_HELP)
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=(
            f"Thread pool count forced onto OMP/BLAS/Paddle (default: {DEFAULT_THREADS}); "
            "overrides ambient thread environment variables"
        ),
    )
    parser.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        help=(
            "Inference engine: paddle (default) or transformers "
            "(torch-based; often faster on Apple Silicon)"
        ),
    )
    parser.add_argument(
        "--device",
        help='Device for inference (e.g. "cpu", "gpu:0"); default is chosen by the pipeline',
    )
    parser.add_argument(
        "--pipeline-version",
        choices=PIPELINE_VERSION_CHOICES,
        help="PaddleOCR-VL pipeline version (v1, v1.5, v1.6); default is chosen by the library",
    )
    return parser


def locate_generated_markdown(save_dir: Path, expected_stem: str) -> Path | None:
    expected_path = save_dir / f"{expected_stem}.md"
    if expected_path.is_file():
        return expected_path

    candidates = sorted(path for path in save_dir.rglob("*.md") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.threads < 1:
        print("ERROR: --threads must be at least 1.", file=sys.stderr)
        sys.exit(1)
    apply_thread_limit(args.threads)

    pdf_path = require_pdf_path(args.pdf_path)

    output_path = resolve_output_path(pdf_path, args.output, args.output_dir)

    selection = None
    if args.page_range:
        try:
            selection = PageSelection.parse(args.page_range, load_page_count(pdf_path))
        except PageSelectionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    paddleocr = import_or_die("paddleocr", "paddleocr[doc-parser]")
    PaddleOCRVL = paddleocr.PaddleOCRVL

    pipeline_kwargs: dict[str, object] = {}
    if args.engine is not None:
        pipeline_kwargs["engine"] = args.engine
    if args.device is not None:
        pipeline_kwargs["device"] = args.device
    if args.pipeline_version is not None:
        pipeline_kwargs["pipeline_version"] = args.pipeline_version

    with tempfile.TemporaryDirectory(prefix="paddleocr_vl_") as work_dir_raw:
        work_dir = Path(work_dir_raw)

        input_pdf = pdf_path
        if selection is not None:
            # PaddleOCR-VL takes no page argument, so the subset becomes its input.
            try:
                input_pdf = selection.as_extracted_pdf(pdf_path, work_dir / f"{pdf_path.stem}.pdf")
            except PageSelectionError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"INFO: Extracted {len(selection)} pages for parsing.")

        try:
            pipeline = PaddleOCRVL(**pipeline_kwargs)
        except Exception as exc:
            print(f"ERROR: Failed to initialize PaddleOCR-VL pipeline: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            pages_results = list(pipeline.predict(input=str(input_pdf)))
        except Exception as exc:
            print(f"ERROR: PaddleOCR-VL prediction failed: {exc}", file=sys.stderr)
            sys.exit(1)

        if not pages_results:
            print("ERROR: PaddleOCR-VL returned no results.", file=sys.stderr)
            sys.exit(1)

        try:
            merged_results = pipeline.restructure_pages(
                pages_results,
                merge_tables=True,
                relevel_titles=True,
                concatenate_pages=True,
            )
            save_dir = work_dir / "markdown"
            save_dir.mkdir(parents=True, exist_ok=True)
            for result in merged_results:
                result.save_to_markdown(save_path=str(save_dir))
        except Exception as exc:
            print(f"ERROR: Saving Markdown failed: {exc}", file=sys.stderr)
            sys.exit(1)

        generated_md = locate_generated_markdown(save_dir, pdf_path.stem)
        if generated_md is None:
            print(
                f"ERROR: PaddleOCR-VL did not produce a Markdown file in: {save_dir}",
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
