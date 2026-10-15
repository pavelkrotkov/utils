#!/usr/bin/env python3
"""Keep local transcription workers within a conservative desktop memory budget.

The lightweight parent samples the worker and descendants independently of the
model's Python GIL. A shared lock prevents simultaneous Whisper/VibeVoice loads.
Help and JSON re-export avoid model loading and therefore need no memory guard.
macOS uses system pressure and physical footprint (including Metal allocations);
other platforms use RSS and a system reserve. Neither is a hard allocation quota.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    import psutil

THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
WORKER_ENV = "UTILS_AUDIO_RESOURCE_WORKER"
PRESSURE_NORMAL, PRESSURE_WARNING, PRESSURE_CRITICAL = 1, 2, 4
WARNING_WINDOW = 10.0
WARNING_GRACE = 5.0
EMERGENCY_GRACE = 0.1


class _RUsageInfoV0(ctypes.Structure):
    """Stable macOS sys/resource.h ABI; sizes and footprint are byte counts."""

    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
        )
    ]


class MacMemory:
    """Read pressure and per-process footprint without importing MLX in the parent.

    Footprint includes compressed/private and IOKit memory, avoiding both RSS's
    Metal blind spot and charging clean file cache as private model memory.
    The pressure sysctl returns dispatch masks 1/2/4, not kernel enum 0/1/3.
    """

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.libc.sysctlbyname.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.libc.sysctlbyname.restype = ctypes.c_int
        self.libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_RUsageInfoV0),
        ]
        self.libproc.proc_pid_rusage.restype = ctypes.c_int

    def pressure(self) -> int:
        value = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if self.libc.sysctlbyname(
            b"kern.memorystatus_vm_pressure_level",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            0,
        ):
            raise OSError(ctypes.get_errno(), "Cannot read macOS memory pressure")
        if size.value != ctypes.sizeof(value) or value.value not in (
            PRESSURE_NORMAL,
            PRESSURE_WARNING,
            PRESSURE_CRITICAL,
        ):
            raise OSError("Unknown macOS memory pressure reading")
        return value.value

    def footprint(self, pid: int) -> int:
        info = _RUsageInfoV0()
        if self.libproc.proc_pid_rusage(pid, 0, ctypes.byref(info)):
            error = ctypes.get_errno()
            if error == errno.ESRCH:  # descendant exited between enumeration and read
                return 0
            raise OSError(error, f"Cannot read memory footprint for PID {pid}")
        return info.phys_footprint


def macos_memory() -> MacMemory | None:
    return MacMemory() if sys.platform == "darwin" else None


class PressureWindow:
    """Bound repeated warnings without resetting on a single normal sample."""

    def __init__(self) -> None:
        self.intervals: deque[tuple[float, float]] = deque()
        self.previous: tuple[float, int] | None = None

    def exceeded(self, now: float, pressure: int) -> bool:
        if self.previous is not None and self.previous[1] == PRESSURE_WARNING:
            self.intervals.append((self.previous[0], now))
        self.previous = now, pressure
        cutoff = now - WARNING_WINDOW
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        exposure = sum(end - max(start, cutoff) for start, end in self.intervals)
        return pressure == PRESSURE_WARNING and exposure >= WARNING_GRACE


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


def stop_worker(worker: subprocess.Popen, grace: float = 2.0) -> None:
    """Stop the whole session, including uv and native descendants."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
        worker.wait(timeout=grace)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGKILL)
    worker.wait()


def process_memory(process: psutil.Process) -> int:
    """A child may exit between enumeration and its RSS read."""
    import psutil

    with contextlib.suppress(psutil.NoSuchProcess):
        return process.memory_info().rss
    return 0


def wait_for_worker(
    worker: subprocess.Popen,
    budget: int,
    reserve: int,
    mac: MacMemory | None = None,
) -> int:
    """Sample outside the worker so inference cannot block the memory guard.

    Sampling can overshoot and cannot provide a hard OS allocation quota.
    """
    import psutil

    try:
        process = psutil.Process(worker.pid)
    except psutil.NoSuchProcess:
        return worker.wait()
    warnings = PressureWindow()
    peak = 0
    while True:
        try:
            result = worker.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(psutil.NoSuchProcess):
                pressure = mac.pressure() if mac else PRESSURE_NORMAL
                reason = "critical macOS memory pressure" if pressure == PRESSURE_CRITICAL else ""
                if mac and warnings.exceeded(time.monotonic(), pressure):
                    reason = "persistent macOS memory pressure warning"
                processes = [process, *process.children(recursive=True)]
                used = sum(mac.footprint(p.pid) if mac else process_memory(p) for p in processes)
                peak = max(peak, used)
                available = psutil.virtual_memory().available
                if not reason and (used > budget or (not mac and available < reserve)):
                    reason = "memory budget reached"
                if reason:
                    print(
                        f"ERROR: Transcription {reason}; stopping worker "
                        f"({'footprint' if mac else 'RSS'} {used / 2**30:.2f} GiB, "
                        f"available {available / 2**30:.2f} GiB).",
                        file=sys.stderr,
                    )
                    return 1
        else:
            if mac:
                print(
                    f"INFO: Sampled peak worker footprint: {peak / 2**30:.2f} GiB.", file=sys.stderr
                )
            return result


def run_worker(lock: TextIO) -> int:
    """Block cancellation until the child's session exists to avoid orphans.

    Unreadable pressure or memory must fail closed.
    """
    import psutil

    memory = psutil.virtual_memory()
    budget, reserve = memory.total // 2, memory.total // 8
    mac = macos_memory()
    if mac and mac.pressure() != PRESSURE_NORMAL:
        print(
            "ERROR: macOS memory pressure is elevated; wait before starting transcription.",
            file=sys.stderr,
        )
        return 1
    if not mac and memory.available < reserve:
        print("ERROR: Not enough available memory to safely start transcription.", file=sys.stderr)
        return 1
    policy = (
        f"{budget / 2**30:.1f} GiB footprint budget, macOS pressure monitoring"
        if mac
        else f"{budget / 2**30:.1f} GiB RSS budget, {reserve / 2**30:.1f} GiB system reserve"
    )
    print(f"INFO: Resource guard: {THREADS} CPU threads, {policy}", file=sys.stderr)
    env = dict(os.environ, **{WORKER_ENV: str(os.getpid())})
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    worker = None
    grace = 2.0
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
        result = wait_for_worker(worker, budget, reserve, mac=mac)
        if mac and result == 1:
            grace = EMERGENCY_GRACE
        return result
    except KeyboardInterrupt:
        return 130
    except (OSError, psutil.Error) as exc:
        print(f"ERROR: Cannot enforce transcription resource guard: {exc}", file=sys.stderr)
        if mac:
            grace = EMERGENCY_GRACE
        return 1
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        if worker is not None:
            stop_worker(worker, grace=grace)
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
