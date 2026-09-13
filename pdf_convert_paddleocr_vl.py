#!/usr/bin/env python3
# /// script
# dependencies = ["paddlepaddle>=3.2", "paddleocr[doc-parser]>=3.6", "pypdf", "psutil>=5.9"]
# ///
"""
Convert a local PDF to Markdown using PaddleOCR-VL.

The first run downloads the layout and VLM models automatically. Defaults are
conservative for a 16 GB Apple Silicon Mac: 4 CPU threads, page/layout/VLM
batches of 1, and asynchronous queues off. Thread limits reduce contention;
batch and queue limits bound memory-bearing concurrency. Results stream directly
to Markdown without cross-page restructuring.

Usage:
    uv run ./pdf_convert_paddleocr_vl.py input.pdf
    uv run ./pdf_convert_paddleocr_vl.py input.pdf --page-range 1-5
    uv run ./pdf_convert_paddleocr_vl.py input.pdf --page-batch-size 2 --layout-batch-size 2 --vlm-batch-size 2 --queues
    ./pdf_convert_paddleocr_vl.py input.pdf --threads 4 --device cpu
    uv run ./pdf_convert_paddleocr_vl.py input.pdf --memory-interval 10 --memory-abort-percent 90
    uv run ./pdf_convert_paddleocr_vl.py input.pdf --mlx-vlm-url http://localhost:8111/
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys
from pathlib import Path

import pdf_paddleocr_supervisor as supervisor
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
DEFAULT_PIPELINE_VERSION = "v1.6"
PIPELINE_NAMES = {"v1": "PaddleOCR-VL", "v1.5": "PaddleOCR-VL-1.5", "v1.6": "PaddleOCR-VL-1.6"}
DEFAULT_THREADS = 4  # PaddleOCR defaults CPU inference to 10.
DEFAULT_BATCH_SIZE = 1  # PaddleX defaults page/layout batches to 64/8.
# Thread pool sizes read by OpenMP, BLAS libraries, NumExpr, PaddlePaddle,
# and Accelerate (macOS).
THREAD_LIMIT_ENV_VARS = [
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "CPU_NUM",
    "VECLIB_MAXIMUM_THREADS",
]


def apply_thread_limit(threads: int) -> None:
    """Force numeric thread pools before the framework libraries load."""
    for env_var in THREAD_LIMIT_ENV_VARS:
        os.environ[env_var] = str(threads)


def _pipeline_kwargs(args: argparse.Namespace) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "engine": args.engine,
        "device": args.device,
        "pipeline_version": args.pipeline_version,
        "cpu_threads": args.threads,
        "use_queues": args.queues,  # PaddleX defaults asynchronous queues on.
    }
    mlx_vlm_url = getattr(args, "mlx_vlm_url", None)
    if mlx_vlm_url:
        kwargs.update(
            vl_rec_backend="mlx-vlm-server",
            vl_rec_server_url=mlx_vlm_url,
            vl_rec_api_model_name=(
                args.mlx_vlm_model or f"PaddlePaddle/{PIPELINE_NAMES[args.pipeline_version]}"
            ),
        )
    return kwargs


def _resource_config(paddlex, args: argparse.Namespace):
    config = paddlex.load_pipeline_config(PIPELINE_NAMES[args.pipeline_version])
    config["batch_size"] = args.page_batch_size  # upstream: 64
    config["SubModules"]["LayoutDetection"]["batch_size"] = args.layout_batch_size  # upstream: 8
    config["SubModules"]["VLRecognition"]["batch_size"] = args.vlm_batch_size  # upstream: -1
    return config


def _log_runtime(paddle, args: argparse.Namespace, device: str) -> None:
    paddle_support = "cuda" if paddle.device.is_compiled_with_cuda() else "cpu-only"
    vlm_backend = "mlx-vlm-server" if getattr(args, "mlx_vlm_url", None) else "native"
    print(
        f"INFO: PaddleOCR-VL device={device} engine={args.engine or 'paddle'} "
        f"vlm_backend={vlm_backend} paddle={paddle_support} "
        f"model={PIPELINE_NAMES[args.pipeline_version]} threads={args.threads} "
        f"batches={getattr(args, 'page_batch_size', 1)}/"
        f"{getattr(args, 'layout_batch_size', 1)}/{getattr(args, 'vlm_batch_size', 1)} "
        f"queues={'on' if args.queues else 'off'}"
    )


def _input_pdf(request: ConversionRequest) -> Path:
    if request.selection is None:
        return request.pdf_path
    try:
        path = request.selection.as_extracted_pdf(
            request.pdf_path, request.workspace / f"{request.pdf_path.stem}.pdf"
        )
    except PageSelectionError as exc:
        raise ConversionError(str(exc)) from exc
    print(f"INFO: Extracted {len(request.selection)} pages for parsing.")
    return path


def _validate_memory_args(args: argparse.Namespace) -> None:
    if not math.isfinite(args.memory_interval) or args.memory_interval < 0:
        raise ConversionError("--memory-interval must be finite and non-negative.")
    threshold = args.memory_abort_percent
    if threshold is not None and not 0 < threshold <= 100:
        raise ConversionError("--memory-abort-percent must be between 0 and 100.")


def _validate_mlx_url(url: str | None) -> None:
    if url and not url.startswith(("http://", "https://")):
        raise ConversionError("--mlx-vlm-url must use http:// or https://.")


class PaddleOcrVlBackend(Backend):
    name = "paddleocr_vl"
    description = "Convert a PDF to Markdown using PaddleOCR-VL."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        add = parser.add_argument
        add("--threads", type=int, default=DEFAULT_THREADS, help="CPU inference/thread limit")
        for name in ("page", "layout", "vlm"):
            add(
                f"--{name}-batch-size",
                type=int,
                default=DEFAULT_BATCH_SIZE,
                help=f"{name} batch size (default: {DEFAULT_BATCH_SIZE})",
            )
        add(
            "--queues",
            action="store_true",
            help="enable PaddleX async prefetch queues (default: off; upstream buffers 64 batches)",
        )
        add("--engine", choices=ENGINE_CHOICES, help="VLM inference engine")
        add("--device", help="pipeline device, e.g. cpu or gpu:0")
        add(
            "--pipeline-version",
            choices=PIPELINE_VERSION_CHOICES,
            default=DEFAULT_PIPELINE_VERSION,
        )
        add("--mlx-vlm-url", help="external MLX-VLM server URL")
        add(
            "--mlx-vlm-model",
            help="model ID/path sent to the external MLX-VLM server",
        )
        add(
            "--memory-interval",
            type=float,
            default=0,
            metavar="SECONDS",
            help="memory report interval; 0 disables reports; threshold alone polls every 5s",
        )
        add(
            "--memory-abort-percent",
            type=float,
            metavar="PERCENT",
            help="abort at this system-wide memory usage percentage",
        )

    def validate(self, args: argparse.Namespace) -> None:
        limits = (args.threads, args.page_batch_size, args.layout_batch_size, args.vlm_batch_size)
        if min(limits) < 1:
            raise ConversionError("Thread and batch limits must be at least 1.")
        if (
            args.engine == "transformers"
            and args.device
            and args.device.split(":", 1)[0] not in ("cpu", "gpu")
        ):
            raise ConversionError("--engine transformers supports only cpu or gpu devices.")
        _validate_memory_args(args)
        _validate_mlx_url(args.mlx_vlm_url)
        apply_thread_limit(args.threads)

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        paddleocr = require_module("paddleocr", "paddleocr[doc-parser]")
        paddlex = require_module("paddlex.inference", "paddleocr[doc-parser]")
        paddle = require_module("paddle", "paddlepaddle")
        device_utils = require_module("paddlex.utils.device", "paddleocr[doc-parser]")
        pipeline_kwargs = _pipeline_kwargs(args)
        device = args.device or device_utils.get_default_device()
        pipeline_kwargs["device"] = device
        pipeline_kwargs["paddlex_config"] = _resource_config(paddlex, args)
        input_pdf = _input_pdf(request)
        _log_runtime(paddle, args, device)

        try:
            pipeline = paddleocr.PaddleOCRVL(**pipeline_kwargs)
        except Exception as exc:
            raise ConversionError(f"Failed to initialize PaddleOCR-VL pipeline: {exc}") from exc

        save_dir = request.workspace / "markdown"
        markdown_path = save_dir / f"{request.pdf_path.stem}.md"
        page_count = 0
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            with markdown_path.open("w", encoding="utf-8") as output:
                for result in pipeline.predict_iter(input=str(input_pdf)):
                    page_count += 1
                    markdown = result.markdown
                    text = markdown["markdown_texts"]
                    for image_ref, image in markdown.get("markdown_images", {}).items():
                        text = text.replace(image_ref, f"page_{page_count}/{image_ref}")
                        image_path = save_dir / f"page_{page_count}/{image_ref}"
                        image_path.parent.mkdir(parents=True, exist_ok=True)
                        image.save(image_path)
                    output.write(text + "\n\n")
        except Exception as exc:
            raise ConversionError(f"PaddleOCR-VL conversion failed: {exc}") from exc

        if not page_count:
            raise ConversionError("PaddleOCR-VL returned no results.")
        return MarkdownDirectory(save_dir, expected_stem=request.pdf_path.stem)


def main() -> None:
    if os.environ.pop(supervisor.WORKER_ENV, None):
        supervisor.prepare_worker_signals()
        sys.exit(execute(PaddleOcrVlBackend()))
    signal.signal(signal.SIGHUP, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(supervisor.supervise(sys.argv[1:], PaddleOcrVlBackend(), Path(__file__).resolve()))


if __name__ == "__main__":
    main()
