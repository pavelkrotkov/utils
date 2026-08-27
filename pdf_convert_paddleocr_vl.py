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

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownDirectory,
    Outcome,
    execute,
    require_module,
)
from pdf_page_selection import PageSelectionError

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


class PaddleOcrVlBackend(Backend):
    name = "paddleocr_vl"
    description = "Convert a PDF to Markdown using PaddleOCR-VL."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--threads",
            type=int,
            default=DEFAULT_THREADS,
            help=(
                f"Thread pool count forced onto OMP/BLAS libraries (default: {DEFAULT_THREADS}); "
                "overrides ambient thread environment variables"
            ),
        )
        parser.add_argument(
            "--engine",
            choices=ENGINE_CHOICES,
            help="VLM inference engine (default: PaddleOCR's own default)",
        )
        parser.add_argument(
            "--device",
            help="Device passed to the pipeline, e.g. cpu or gpu:0",
        )
        parser.add_argument(
            "--pipeline-version",
            choices=PIPELINE_VERSION_CHOICES,
            help="PaddleOCR-VL pipeline version (default: PaddleOCR's own default)",
        )

    def validate(self, args: argparse.Namespace) -> None:
        if args.threads < 1:
            raise ConversionError("--threads must be at least 1.")
        # Must land before paddle is imported, so it happens in validate.
        apply_thread_limit(args.threads)

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        paddleocr = require_module("paddleocr", "paddleocr[doc-parser]")

        pipeline_kwargs: dict[str, object] = {}
        if args.engine is not None:
            pipeline_kwargs["engine"] = args.engine
        if args.device is not None:
            pipeline_kwargs["device"] = args.device
        if args.pipeline_version is not None:
            pipeline_kwargs["pipeline_version"] = args.pipeline_version

        input_pdf = request.pdf_path
        if request.selection is not None:
            # PaddleOCR-VL takes no page argument, so the subset becomes its input.
            try:
                input_pdf = request.selection.as_extracted_pdf(
                    request.pdf_path,
                    request.workspace / f"{request.pdf_path.stem}.pdf",
                )
            except PageSelectionError as exc:
                raise ConversionError(str(exc)) from exc
            print(f"INFO: Extracted {len(request.selection)} pages for parsing.")

        try:
            pipeline = paddleocr.PaddleOCRVL(**pipeline_kwargs)
        except Exception as exc:
            raise ConversionError(f"Failed to initialize PaddleOCR-VL pipeline: {exc}") from exc

        try:
            pages_results = list(pipeline.predict(input=str(input_pdf)))
        except Exception as exc:
            raise ConversionError(f"PaddleOCR-VL prediction failed: {exc}") from exc

        if not pages_results:
            raise ConversionError("PaddleOCR-VL returned no results.")

        save_dir = request.workspace / "markdown"
        try:
            merged_results = pipeline.restructure_pages(
                pages_results,
                merge_tables=True,
                relevel_titles=True,
                concatenate_pages=True,
            )
            save_dir.mkdir(parents=True, exist_ok=True)
            for result in merged_results:
                result.save_to_markdown(save_path=str(save_dir))
        except Exception as exc:
            raise ConversionError(f"Saving Markdown failed: {exc}") from exc

        return MarkdownDirectory(save_dir, expected_stem=request.pdf_path.stem)


def main() -> None:
    sys.exit(execute(PaddleOcrVlBackend()))


if __name__ == "__main__":
    main()
