#!/usr/bin/env python3
"""Keep local transcription workers within a conservative desktop memory budget.

The lightweight parent samples the worker and descendants independently of the
model's Python GIL. A shared lock prevents simultaneous Whisper/VibeVoice loads.
Help and JSON re-export avoid model loading and therefore need no memory guard.
This is a sampled RSS/system-reserve guard, not a hard GPU or allocation quota.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
WORKER_ENV = "UTILS_AUDIO_RESOURCE_WORKER"


def configure_threads() -> None:
    """Override ambient pool sizes before importing numerical libraries."""
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        os.environ[name] = str(THREADS)


def stop_worker(worker: subprocess.Popen) -> None:
    """Stop the whole session, including uv and native descendants."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
        worker.wait(timeout=2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGKILL)
    worker.wait()


def process_memory(process) -> int:
    """A child may exit between enumeration and its RSS read."""
    import psutil

    with contextlib.suppress(psutil.NoSuchProcess):
        return process.memory_info().rss
    return 0


def wait_for_worker(worker: subprocess.Popen, budget: int, reserve: int) -> int:
    """Sample outside the worker so inference cannot block the memory guard.

    RSS misses compressed/swapped allocations; system reserve is checked too.
    Sampling can overshoot and cannot provide a hard OS allocation quota.
    """
    import psutil

    try:
        process = psutil.Process(worker.pid)
    except psutil.NoSuchProcess:
        return worker.wait()
    while True:
        try:
            return worker.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(psutil.NoSuchProcess):
                rss = sum(process_memory(p) for p in [process, *process.children(recursive=True)])
                available = psutil.virtual_memory().available
                if rss > budget or available < reserve:
                    print(
                        "ERROR: Transcription memory budget reached; stopping worker "
                        f"(RSS {rss / 2**30:.2f} GiB, available {available / 2**30:.2f} GiB).",
                        file=sys.stderr,
                    )
                    return 1


def run_worker(lock) -> int:
    """Block cancellation until the child's session exists to avoid orphans."""
    import psutil

    memory = psutil.virtual_memory()
    budget, reserve = memory.total // 2, memory.total // 8
    if memory.available < reserve:
        print("ERROR: Not enough available memory to safely start transcription.", file=sys.stderr)
        return 1
    print(
        f"INFO: Resource guard: {THREADS} CPU threads, {budget / 2**30:.1f} GiB RSS budget, "
        f"{reserve / 2**30:.1f} GiB system reserve",
        file=sys.stderr,
    )
    env = dict(os.environ, **{WORKER_ENV: str(os.getpid())})
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    worker = None
    try:
        worker = subprocess.Popen(
            [sys.executable, *sys.argv],
            env=env,
            pass_fds=(lock.fileno(),),
            start_new_session=True,
        )
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(worker.pid).nice(10)
        return wait_for_worker(worker, budget, reserve)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        if worker is not None:
            stop_worker(worker)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def supervise(main: Callable[[], None]) -> None:
    """Hold one per-user model lock until the complete worker session stops."""
    configure_threads()
    if is_worker():
        signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
        prepare_signals()
        try:
            main()
        except KeyboardInterrupt:
            sys.exit(130)
        return
    if model_free_arguments():
        main()
        return
    prepare_signals()
    lock_path = Path(tempfile.gettempdir()) / f"utils-audio-{os.getuid()}.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("ERROR: Another local transcription is already running.")
        try:
            sys.exit(run_worker(lock))
        except (ImportError, OSError) as exc:
            sys.exit(f"ERROR: Cannot enforce transcription resource guard: {exc}")


def is_worker() -> bool:
    """uv re-execution inserts another parent; recognize any supervisor ancestor."""
    if WORKER_ENV not in os.environ:
        return False
    import psutil

    return os.environ[WORKER_ENV] in {str(p.pid) for p in psutil.Process().parents()}


def prepare_signals() -> None:
    """Make TERM interrupt Python so finally blocks can remove temporary audio."""
    for signum in SIGNALS:
        signal.signal(signum, signal.default_int_handler)


def model_free_arguments() -> bool:
    """Respect -- so an input named --help cannot skip the model guard."""
    args = sys.argv[1:]
    if "--" in args:
        args = args[: args.index("--")]
    return bool({"--help", "-h", "--from-json"}.intersection(args))
