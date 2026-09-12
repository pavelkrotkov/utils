#!/usr/bin/env python3
"""Supervise the PaddleOCR-VL worker and optional memory guard.

The parent owns the per-user flock and process group so overlapping model loads
are rejected and interruption cannot orphan descendants. It blocks termination
signals while the child process group is being created, then restores them.

Memory sampling stays in the lightweight parent, so diagnostics remain available
even while PaddleOCR initializes. Polling is disabled unless an interval or abort
threshold is requested. The threshold uses total system memory because Apple MLX
and CPU workloads share unified RAM.
"""

import argparse
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pdf_convert_run import ConversionError, build_parser, execute, require_module

TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
DEFAULT_MEMORY_POLL_SECONDS = 5
LOCK_PATH = Path.home() / ".pdf_convert_paddleocr_vl.lock"
WORKER_ENV = "PDF_CONVERT_PADDLEOCR_VL_WORKER"
HELP_FLAGS = frozenset(("-h", "--help"))


@contextlib.contextmanager
def conversion_lock():
    with LOCK_PATH.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConversionError("Another PaddleOCR-VL conversion is already running.") from exc
        yield lock


def terminate_process_group(worker: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(worker.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
        worker.wait(timeout=2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGKILL)
    worker.wait()


def prepare_worker_signals() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, TERMINATION_SIGNALS)
    for signum in TERMINATION_SIGNALS:
        signal.signal(signum, signal.default_int_handler)


def report_memory(psutil, process, threshold: float | None) -> None:
    with contextlib.suppress(psutil.NoSuchProcess):
        rss = process.memory_info().rss
        memory = psutil.virtual_memory()
        print(
            f"INFO: Memory worker_rss={rss / 2**30:.2f} GiB "
            f"system={memory.percent:.1f}% used available={memory.available / 2**30:.2f} GiB"
        )
        if threshold is not None and memory.percent >= threshold:
            print(
                f"WARNING: System memory reached {memory.percent:.1f}% "
                f"(abort threshold {threshold:.1f}%); stopping conversion.",
                file=sys.stderr,
            )
            raise ConversionError("Memory abort threshold reached.")


def wait_for_worker(worker: subprocess.Popen[bytes], args: argparse.Namespace) -> int:
    interval = args.memory_interval
    if not interval and args.memory_abort_percent is not None:
        interval = DEFAULT_MEMORY_POLL_SECONDS
    if not interval:
        return worker.wait()
    psutil = require_module("psutil", "psutil")
    process = psutil.Process(worker.pid)
    while worker.poll() is None:
        time.sleep(interval)
        report_memory(psutil, process, args.memory_abort_percent)
    return worker.returncode or 0


def run_worker(argv: list[str], lock, args: argparse.Namespace, script: Path) -> int:
    env = os.environ.copy()
    env[WORKER_ENV] = "1"
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    worker = None
    try:
        try:
            worker = subprocess.Popen(
                [sys.executable, str(script), *argv],
                env=env,
                pass_fds=(lock.fileno(),),
                start_new_session=True,
            )
        except OSError as exc:
            raise ConversionError(f"Failed to start PaddleOCR-VL worker: {exc}") from exc
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return wait_for_worker(worker, args)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
        if worker is not None and worker.returncode != 0:
            terminate_process_group(worker)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def supervise(argv: list[str], backend, script: Path) -> int:
    help_args = argv[: argv.index("--")] if "--" in argv else argv
    if HELP_FLAGS.intersection(help_args):
        return execute(backend)
    args = build_parser(backend).parse_args(argv)
    try:
        backend.validate(args)
        with conversion_lock() as lock:
            return run_worker(argv, lock, args, script)
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
