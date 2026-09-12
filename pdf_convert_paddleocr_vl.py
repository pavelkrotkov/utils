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
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

from pdf_convert_run import (
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownDirectory,
    Outcome,
    build_parser,
    execute,
    require_module,
)
from pdf_page_selection import PageSelectionError

ENGINE_CHOICES = ("paddle", "transformers")
PIPELINE_VERSION_CHOICES = ("v1", "v1.5", "v1.6")
DEFAULT_PIPELINE_VERSION = "v1.6"
PIPELINE_NAMES = {
    "v1": "PaddleOCR-VL",
    "v1.5": "PaddleOCR-VL-1.5",
    "v1.6": "PaddleOCR-VL-1.6",
}
HELP_FLAGS = frozenset(("-h", "--help"))
TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
DEFAULT_THREADS = 4  # PaddleOCR defaults CPU inference to 10.
DEFAULT_BATCH_SIZE = 1  # PaddleX defaults page/layout batches to 64/8.
DEFAULT_MEMORY_POLL_SECONDS = 5
LOCK_PATH = Path.home() / ".pdf_convert_paddleocr_vl.lock"
WORKER_ENV = "PDF_CONVERT_PADDLEOCR_VL_WORKER"
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


def _conversion_lock():
    lock = LOCK_PATH.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ConversionError("Another PaddleOCR-VL conversion is already running.") from exc
    except Exception:
        lock.close()
        raise
    return lock


def _terminate_process_group(worker: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(worker.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
        worker.wait(timeout=2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGKILL)
    worker.wait()


def _prepare_worker_signals() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, TERMINATION_SIGNALS)
    for signum in TERMINATION_SIGNALS:
        signal.signal(signum, signal.default_int_handler)


def _wait_for_worker(worker: subprocess.Popen[bytes], args: argparse.Namespace) -> int:
    interval = args.memory_interval or (
        DEFAULT_MEMORY_POLL_SECONDS if args.memory_abort_percent is not None else 0
    )
    if not interval:
        return worker.wait()

    psutil = require_module("psutil", "psutil")
    process = psutil.Process(worker.pid)
    while True:
        try:
            return worker.wait(timeout=interval)
        except subprocess.TimeoutExpired:
            pass
        try:
            rss = process.memory_info().rss
        except psutil.NoSuchProcess:
            return worker.wait()
        memory = psutil.virtual_memory()
        print(
            f"INFO: Memory worker_rss={rss / 2**30:.2f} GiB "
            f"system={memory.percent:.1f}% used available={memory.available / 2**30:.2f} GiB"
        )
        if args.memory_abort_percent is not None and memory.percent >= args.memory_abort_percent:
            print(
                f"WARNING: System memory reached {memory.percent:.1f}% "
                f"(abort threshold {args.memory_abort_percent:.1f}%); stopping conversion.",
                file=sys.stderr,
            )
            raise ConversionError("Memory abort threshold reached.")


def _run_worker(argv: list[str], lock, args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env[WORKER_ENV] = "1"
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    worker = None
    try:
        try:
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), *argv],
                env=env,
                pass_fds=(lock.fileno(),),
                start_new_session=True,
            )
        except OSError as exc:
            raise ConversionError(f"Failed to start PaddleOCR-VL worker: {exc}") from exc
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return _wait_for_worker(worker, args)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
        if worker is not None and worker.returncode != 0:
            _terminate_process_group(worker)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _supervise(argv: list[str]) -> int:
    help_args = argv[: argv.index("--")] if "--" in argv else argv
    if HELP_FLAGS.intersection(help_args):
        return execute(PaddleOcrVlBackend())
    backend = PaddleOcrVlBackend()
    args = build_parser(backend).parse_args(argv)
    try:
        backend.validate(args)
        lock = _conversion_lock()
        with lock:
            return _run_worker(argv, lock, args)
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


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
            vl_rec_api_model_name=f"PaddlePaddle/{PIPELINE_NAMES[args.pipeline_version]}",
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
    if args.memory_interval < 0:
        raise ConversionError("--memory-interval cannot be negative.")
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
        parser.add_argument(
            "--threads",
            type=int,
            default=DEFAULT_THREADS,
            help=f"CPU inference/thread-pool limit (default: {DEFAULT_THREADS})",
        )
        for name, label in (("page", "PDF page"), ("layout", "layout"), ("vlm", "VLM")):
            parser.add_argument(
                f"--{name}-batch-size",
                type=int,
                default=DEFAULT_BATCH_SIZE,
                help=f"{label} batch size (default: {DEFAULT_BATCH_SIZE})",
            )
        # ponytail: upstream exposes queue enablement but not its hard-coded 64-batch depth.
        parser.add_argument(
            "--queues",
            action="store_true",
            help="enable PaddleX async prefetch queues (default: off; upstream buffers 64 batches)",
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
            default=DEFAULT_PIPELINE_VERSION,
            help=f"PaddleOCR-VL pipeline version (default: {DEFAULT_PIPELINE_VERSION})",
        )
        parser.add_argument(
            "--mlx-vlm-url",
            help="delegate VLM recognition to an external MLX-VLM server",
        )
        parser.add_argument(
            "--memory-interval",
            type=float,
            default=0,
            metavar="SECONDS",
            help="periodic worker/system memory report interval; 0 disables reports",
        )
        parser.add_argument(
            "--memory-abort-percent",
            type=float,
            metavar="PERCENT",
            help="abort when system memory usage reaches this percentage",
        )

    def validate(self, args: argparse.Namespace) -> None:
        if (
            min(
                args.threads,
                args.page_batch_size,
                args.layout_batch_size,
                args.vlm_batch_size,
            )
            < 1
        ):
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
    if os.environ.pop(WORKER_ENV, None):
        _prepare_worker_signals()
        sys.exit(execute(PaddleOcrVlBackend()))
    signal.signal(signal.SIGHUP, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(_supervise(sys.argv[1:]))


if __name__ == "__main__":
    main()
