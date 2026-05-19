#!/usr/bin/env python3
"""Shared progress reporting helpers for audio transcription scripts."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Optional


def format_duration(seconds: Optional[float]) -> str:
    """Format a duration in seconds as MM:SS or H:MM:SS."""
    if seconds is None:
        return "unknown"

    try:
        seconds_float = float(seconds)
    except (TypeError, ValueError):
        return "unknown"

    if seconds_float < 0:
        return "unknown"

    seconds_int = int(round(seconds_float))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class ProgressReporter:
    """Print throttled, line-oriented progress reports to stderr."""

    def __init__(self, enabled: bool = True, interval: float = 10.0):
        self.enabled = enabled
        self.interval = max(0.5, interval)
        self.starts: dict[str, float] = {}
        self.last_updates: dict[str, float] = {}

    def info(self, message: str) -> None:
        if self.enabled:
            print(f"INFO: {message}", file=sys.stderr, flush=True)

    def start(self, stage: str, detail: Optional[str] = None) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        self.starts[stage] = now
        # Force the first update through throttling after a stage starts.
        self.last_updates[stage] = 0.0

        message = f"{stage} started"
        if detail:
            message = f"{message} ({detail})"
        print(f"INFO: {message}", file=sys.stderr, flush=True)

    def update(
        self,
        stage: str,
        completed: Optional[float] = None,
        total: Optional[float] = None,
        *,
        detail: Optional[str] = None,
        force: bool = False,
        show_count: bool = True,
    ) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        self.starts.setdefault(stage, now)
        last_update = self.last_updates.get(stage, 0.0)
        if not force and now - last_update < self.interval:
            return

        self.last_updates[stage] = now
        elapsed = now - self.starts[stage]
        parts: list[str] = []

        if completed is not None and total is not None and total > 0:
            bounded_completed = min(max(float(completed), 0.0), float(total))
            percent = 100.0 * bounded_completed / float(total)
            parts.append(f"{percent:5.1f}%")

            if show_count:
                parts.append(f"({bounded_completed:g}/{float(total):g})")

            eta = None
            if bounded_completed > 0 and bounded_completed < total:
                eta = elapsed * (float(total) - bounded_completed) / bounded_completed

            parts.append(f"elapsed {format_duration(elapsed)}")
            parts.append(f"ETA {format_duration(eta)}")

        elif completed is not None:
            parts.append(f"processed {format_duration(completed)}")
            parts.append(f"elapsed {format_duration(elapsed)}")

        else:
            parts.append(f"elapsed {format_duration(elapsed)}")

        if detail:
            parts.append(detail)

        print(f"INFO: {stage}: {', '.join(parts)}", file=sys.stderr, flush=True)

    def finish(self, stage: str, detail: Optional[str] = None) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        start = self.starts.get(stage, now)
        message = f"{stage} finished in {format_duration(now - start)}"
        if detail:
            message = f"{message} ({detail})"
        print(f"INFO: {message}", file=sys.stderr, flush=True)


def print_process_tail(lines: Sequence[str], label: str) -> None:
    """Print a short tail from captured process output after a failure."""
    if not lines:
        return

    print(f"ERROR: Last {label} output lines:", file=sys.stderr)
    for line in lines[-20:]:
        print(f"  {line}", file=sys.stderr)


def parse_ffmpeg_timestamp(value: str) -> Optional[float]:
    """Parse ffmpeg progress timestamps like HH:MM:SS.microseconds."""
    if not value or value == "N/A":
        return None

    parts = value.split(":")
    if len(parts) != 3:
        return None

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None

    return hours * 3600 + minutes * 60 + seconds


def probe_media_duration(
    input_path: Path,
    ffprobe_bin: str,
    verbose: bool = False,
) -> Optional[float]:
    """Return media duration in seconds when ffprobe is available."""
    if not shutil.which(ffprobe_bin) and not Path(ffprobe_bin).exists():
        if verbose:
            print(f"WARNING: ffprobe binary not found: {ffprobe_bin}", file=sys.stderr)
        return None

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        if verbose:
            print(f"WARNING: Could not probe media duration: {input_path}", file=sys.stderr)
        return None

    try:
        duration = float(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None

    return duration if duration > 0 else None


def validate_nonempty_output(path: Path, label: str) -> None:
    """Exit when a subprocess claims success but leaves no meaningful output."""
    if not path.exists():
        print(f"ERROR: {label} did not create expected output: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        size = path.stat().st_size
    except OSError as e:
        print(f"ERROR: Could not inspect {label} output {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if size <= 128:
        print(f"ERROR: {label} output is empty or too small: {path}", file=sys.stderr)
        sys.exit(1)


def run_with_progress(
    cmd: Sequence[str],
    label: str,
    parse_progress_line: Callable[[str], Optional[float]],
    *,
    reporter: Optional[ProgressReporter] = None,
    verbose: bool = False,
    start_detail: Optional[str] = None,
    finish_detail: Optional[str] = None,
    missing_binary_label: Optional[str] = None,
    force_final_percent: bool = False,
    popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> list[str]:
    """Run a subprocess and map output lines to optional 0-100 progress percentages."""
    if reporter:
        reporter.start(label, detail=start_detail)

    output_tail: list[str] = []
    last_percent: Optional[float] = None

    try:
        process = popen_factory(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError:
        binary = missing_binary_label or (str(cmd[0]) if cmd else label)
        print(f"ERROR: {label} binary not found or not executable: {binary}", file=sys.stderr)
        sys.exit(1)

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue

        percent = parse_progress_line(line)
        if percent is not None:
            last_percent = percent
            if reporter:
                reporter.update(
                    label,
                    completed=percent,
                    total=100.0,
                    force=percent >= 100.0,
                    show_count=False,
                )
            continue

        output_tail.append(line)
        if verbose:
            print(line, file=sys.stderr)

    process.stdout.close()

    returncode = process.wait()
    if returncode != 0:
        print_process_tail(output_tail, label)
        print(f"ERROR: {label} failed with exit code {returncode}", file=sys.stderr)
        sys.exit(1)

    if force_final_percent and (last_percent is None or last_percent < 100.0) and reporter:
        reporter.update(
            label,
            completed=100.0,
            total=100.0,
            force=True,
            show_count=False,
        )

    if reporter:
        reporter.finish(label, detail=finish_detail)

    return output_tail


def convert_to_pcm16k_mono(
    input_path: Path,
    output_wav: Path,
    *,
    progress: Optional[ProgressReporter] = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    verbose: bool = False,
) -> None:
    """Convert input media to mono 16 kHz WAV using ffmpeg."""
    duration = probe_media_duration(input_path, ffprobe_bin, verbose) if progress else None
    saw_ffmpeg_progress = False

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
    ]
    if not verbose:
        cmd.extend(["-loglevel", "error"])

    if progress:
        cmd.extend(["-progress", "pipe:1", "-nostats"])

    cmd.extend(
        [
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            str(output_wav),
        ]
    )

    if verbose:
        print(f"INFO: Running ffmpeg: {' '.join(cmd)}", file=sys.stderr)

    def parse_progress(line: str) -> Optional[float]:
        nonlocal saw_ffmpeg_progress
        if "=" not in line:
            return None

        key, value = line.split("=", 1)
        processed_seconds: Optional[float] = None
        if key in {"out_time_ms", "out_time_us"}:
            try:
                processed_seconds = int(value) / 1_000_000.0
            except ValueError:
                return None
        elif key == "out_time":
            processed_seconds = parse_ffmpeg_timestamp(value)
        elif key == "progress" and value == "end" and duration and not saw_ffmpeg_progress:
            return 100.0

        if processed_seconds is None or not duration:
            return None

        saw_ffmpeg_progress = True
        return 100.0 * min(processed_seconds, duration) / duration

    detail = f"duration {format_duration(duration)}" if duration else "duration unknown"
    output_tail = run_with_progress(
        cmd,
        "ffmpeg conversion",
        parse_progress,
        reporter=progress,
        verbose=verbose,
        start_detail=detail if progress else None,
        finish_detail=str(output_wav) if progress else None,
        missing_binary_label=ffmpeg_bin,
    )

    try:
        validate_nonempty_output(output_wav, "ffmpeg")
    except SystemExit:
        print_process_tail(output_tail, "ffmpeg conversion")
        raise


def run_threaded_with_periodic_progress(
    func: Callable[[], None],
    *,
    reporter: ProgressReporter,
    label: str,
    interval: float,
) -> None:
    """Run blocking Python work while periodically emitting indeterminate progress."""
    done = threading.Event()
    error: list[BaseException] = []

    def target() -> None:
        try:
            func()
        except BaseException as exc:
            error.append(exc)
        finally:
            done.set()

    reporter.start(label)
    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    ticks = 0
    while not done.wait(max(0.5, interval)):
        ticks += 1
        percent = min(95.0, 5.0 + ticks * 2.5)
        reporter.update(
            label,
            completed=percent,
            total=100.0,
            detail="still running",
            force=True,
            show_count=False,
        )

    thread.join()
    if error:
        raise error[0]

    reporter.update(label, completed=100.0, total=100.0, force=True, show_count=False)
    reporter.finish(label)
