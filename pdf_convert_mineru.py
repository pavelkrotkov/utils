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
from pathlib import Path

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownDirectory,
    Outcome,
    execute,
)
from pdf_page_selection import PageSelectionError

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


def locate_mineru_binary() -> str:
    """Return the path to the ``mineru`` executable.

    Raises:
        ConversionError: the executable is not in this environment.
    """
    candidate = shutil.which("mineru")
    if candidate is not None:
        return candidate

    local_candidate = Path(sys.executable).with_name("mineru")
    if local_candidate.is_file():
        return str(local_candidate)

    raise ConversionError("The 'mineru' executable was not found in this environment.")


class MinerUBackend(Backend):
    name = "mineru"
    description = "Convert a PDF to Markdown using MinerU."
    page_range_caveat = "MinerU supports contiguous ranges only."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
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

    def validate(self, args: argparse.Namespace) -> None:
        if args.threads < 1:
            raise ConversionError("--threads must be at least 1.")
        # Must land before the MinerU process is spawned, so it happens here.
        apply_thread_limit(args.threads)

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        mineru_bin = locate_mineru_binary()

        command = [mineru_bin, "-p", str(request.pdf_path), "-o", str(request.workspace)]

        if request.selection is not None:
            try:
                first_page, last_page = request.selection.as_contiguous()
            except PageSelectionError as exc:
                raise ConversionError(
                    f"MinerU supports contiguous page ranges only; {exc}"
                ) from exc
            # MinerU's -s/-e are 0-based and inclusive.
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
            raise ConversionError(f"MinerU exited with status {exc.returncode}: {exc}") from exc
        except OSError as exc:
            raise ConversionError(f"Failed to launch MinerU: {exc}") from exc

        # MinerU nests its output as <stem>/<method>/<stem>.md, so the run
        # module falls through to the single-unambiguous-Markdown rule.
        return MarkdownDirectory(request.workspace, expected_stem=request.pdf_path.stem)


def main() -> None:
    sys.exit(execute(MinerUBackend()))


if __name__ == "__main__":
    main()
