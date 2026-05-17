#!/usr/bin/env python3
# /// script
# dependencies = [
#   "torch",
#   "pyannote.audio",
# ]
# ///
"""
audio_transcribe_whisper.py - Robust whisper-cpp ASR with optional pyannote diarization

Converts input media to mono 16 kHz WAV (via ffmpeg), runs whisper-cpp for ASR (JSON output),
and writes a plain-text transcript. When requested, runs pyannote.audio for speaker diarization,
then merges results into a transcript with speaker labels or break markers (no timestamps).

Designed for resilience: handles diverse JSON formats from different whisper-cpp builds,
gracefully falls back when timestamps/diarization are unavailable, and auto-configures
Metal acceleration on macOS.

Dependencies:
  - ffmpeg, whisper-cpp (CLI binaries)
  - Python: torch, pyannote.audio, argparse, json, pathlib, subprocess, tempfile, os, sys

Usage Examples:
  ./audio_transcribe_whisper.py input.m4a
  ./audio_transcribe_whisper.py input.m4a --format srt
  ./audio_transcribe_whisper.py input.m4a --diarization --speakers "Alice,Bob" --num-speakers 2
  ./audio_transcribe_whisper.py input.m4a --diarization --style breaks
  ./audio_transcribe_whisper.py input.m4a --diarization --no-ffmpeg --pyannote-model pyannote/speaker-diarization-community-1
"""

import argparse
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from audio_transcript import TranscriptSegment, emit_transcript, remap_speakers

try:
    from pyannote.audio import Pipeline
    from pyannote.core import Annotation
except ImportError as e:
    print(f"ERROR: Missing required Python package: {e}", file=sys.stderr)
    print("Install with: pip install torch pyannote.audio", file=sys.stderr)
    sys.exit(1)


# ───────────────────────────────────────────────────────────────────────────────
# Environment setup
# ───────────────────────────────────────────────────────────────────────────────


def maybe_set_metal_env() -> None:
    """
    On macOS with Homebrew whisper-cpp, auto-set GGML_METAL_PATH_RESOURCES if unset.
    """
    if platform.system() != "Darwin":
        return
    if "GGML_METAL_PATH_RESOURCES" in os.environ:
        return
    try:
        brew_prefix = subprocess.check_output(
            ["brew", "--prefix", "whisper-cpp"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        metal_path = Path(brew_prefix) / "share" / "whisper-cpp"
        if metal_path.exists():
            os.environ["GGML_METAL_PATH_RESOURCES"] = str(metal_path)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


# ───────────────────────────────────────────────────────────────────────────────
# Progress reporting
# ───────────────────────────────────────────────────────────────────────────────


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
        self.starts: Dict[str, float] = {}
        self.last_updates: Dict[str, float] = {}

    def info(self, message: str) -> None:
        if self.enabled:
            print(f"INFO: {message}", file=sys.stderr, flush=True)

    def start(self, stage: str, detail: Optional[str] = None) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        self.starts[stage] = now
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
        parts: List[str] = []

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


class PyannoteProgressHook:
    """Bridge pyannote pipeline hooks to ProgressReporter."""

    def __init__(self, reporter: ProgressReporter):
        self.reporter = reporter
        self.completed_steps: set[str] = set()

    def __enter__(self) -> "PyannoteProgressHook":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def __call__(
        self,
        step_name: str,
        step_artifact: Any,
        file: Optional[Dict[str, Any]] = None,
        total: Optional[int] = None,
        completed: Optional[int] = None,
    ) -> None:
        del step_artifact, file

        stage = f"pyannote {step_name.replace('_', ' ')}"
        if completed is not None and total is not None:
            self.reporter.update(
                stage,
                completed=completed,
                total=total,
                force=completed == 0 or completed >= total,
            )
            if completed >= total:
                self.completed_steps.add(step_name)
            return

        if step_name not in self.completed_steps:
            self.reporter.update(stage, detail="complete", force=True)
            self.completed_steps.add(step_name)


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


def print_process_tail(lines: List[str], label: str) -> None:
    """Print a short tail from captured process output after a failure."""
    if not lines:
        return

    print(f"ERROR: Last {label} output lines:", file=sys.stderr)
    for line in lines[-20:]:
        print(f"  {line}", file=sys.stderr)


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


# ───────────────────────────────────────────────────────────────────────────────
# JSON robustness: whisper-cpp output parsing
# ───────────────────────────────────────────────────────────────────────────────


def _has_time(seg: Any) -> bool:
    """Check if segment (dict or string) has any timing keys."""
    if not isinstance(seg, dict):
        return False
    if any(k in seg for k in ["start", "end", "t0", "t1", "ts", "te"]):
        return True
    # Check whisper-cpp offsets format
    if "offsets" in seg and isinstance(seg["offsets"], dict):
        return "from" in seg["offsets"] or "to" in seg["offsets"]
    return False


def _seg_start(seg: Dict[str, Any]) -> Optional[float]:
    """Extract start time (seconds) from segment, or None if unavailable."""
    if "start" in seg:
        return float(seg["start"])
    if "t0" in seg:
        return float(seg["t0"]) * 0.01  # centiseconds
    if "ts" in seg:
        return float(seg["ts"])
    # whisper-cpp format: offsets.from in milliseconds
    if "offsets" in seg and isinstance(seg["offsets"], dict):
        if "from" in seg["offsets"]:
            return float(seg["offsets"]["from"]) * 0.001
    return None


def _seg_end(seg: Dict[str, Any]) -> Optional[float]:
    """Extract end time (seconds) from segment, or None if unavailable."""
    if "end" in seg:
        return float(seg["end"])
    if "t1" in seg:
        return float(seg["t1"]) * 0.01  # centiseconds
    if "te" in seg:
        return float(seg["te"])
    # whisper-cpp format: offsets.to in milliseconds
    if "offsets" in seg and isinstance(seg["offsets"], dict):
        if "to" in seg["offsets"]:
            return float(seg["offsets"]["to"]) * 0.001
    return None


def _seg_text(seg: Any) -> str:
    """Extract text from segment (dict or string). Handle multiple text keys."""
    if isinstance(seg, str):
        return seg
    if isinstance(seg, dict):
        for key in ["text", "content", "utterance"]:
            if key in seg:
                return str(seg[key]).strip()
    return ""


def _normalize_segments(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalize whisper JSON 'segments' field to a list of dicts.
    Handles:
      - list of dicts
      - dict keyed by numeric strings ("0", "1", ...)
      - nested lists (flatten)
      - plain strings (wrap as {"text": "..."})
    """
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, list):
                # Nested list: flatten
                result.extend(_normalize_segments(item))
            elif isinstance(item, str):
                result.append({"text": item})
            elif isinstance(item, dict):
                result.append(item)
            else:
                # Unknown type: wrap as text
                result.append({"text": str(item)})
        return result

    if isinstance(raw, dict):
        # Try numeric key ordering
        keys = list(raw.keys())
        try:
            keys_sorted = sorted(keys, key=int)
            return [
                raw[k] if isinstance(raw[k], dict) else {"text": str(raw[k])} for k in keys_sorted
            ]
        except ValueError:
            # Not numeric keys; just use values
            return [v if isinstance(v, dict) else {"text": str(v)} for v in raw.values()]

    # Single item or unknown
    if isinstance(raw, str):
        return [{"text": raw}]
    if isinstance(raw, dict):
        return [raw]
    return []


def load_whisper_segments(
    json_path: Path, verbose: bool = False
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Load and normalize whisper-cpp JSON output.
    Returns: (segments_list, fallback_text)
      - segments_list: normalized list of segment dicts
      - fallback_text: top-level "text" field if present, else None
    Raises SystemExit on errors.
    """
    if not json_path.exists():
        print(f"ERROR: Whisper JSON not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in {json_path}: {e}", file=sys.stderr)
            sys.exit(1)

    fallback_text = data.get("text")
    raw_segments = data.get("segments") or data.get("transcription")

    if raw_segments is None:
        if fallback_text:
            if verbose:
                print(
                    "INFO: No 'segments'/'transcription' field, using top-level 'text'",
                    file=sys.stderr,
                )
            return [], fallback_text
        print(
            f"ERROR: Whisper JSON has no 'segments'/'transcription' and no 'text': {json_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    segments = _normalize_segments(raw_segments)
    if not segments and not fallback_text:
        print(
            f"ERROR: Whisper JSON 'segments' is empty and no 'text': {json_path}", file=sys.stderr
        )
        sys.exit(1)

    # Sort by time if any segment has timestamps
    if any(_has_time(s) for s in segments):

        def sort_key(s: Dict[str, Any]) -> Tuple[float, float]:
            start = _seg_start(s)
            end = _seg_end(s)
            return (start if start is not None else 0.0, end if end is not None else 0.0)

        segments.sort(key=sort_key)

    return segments, fallback_text


# ───────────────────────────────────────────────────────────────────────────────
# Pyannote diarization
# ───────────────────────────────────────────────────────────────────────────────


def print_pyannote_access_help() -> None:
    """Print actionable help for common Hugging Face access issues."""
    print("To fix pyannote model access:", file=sys.stderr)
    print("1) Log in to Hugging Face with the account tied to your HF_TOKEN.", file=sys.stderr)
    print("2) Open and accept model terms:", file=sys.stderr)
    print("   - https://huggingface.co/pyannote/speaker-diarization-3.1", file=sys.stderr)
    print(
        "   - https://huggingface.co/pyannote/speaker-diarization-community-1",
        file=sys.stderr,
    )
    print("3) Ensure HF_TOKEN has read permission.", file=sys.stderr)
    print(
        "Note: pyannoteAI is a hosted service; this script uses local pyannote.audio models from Hugging Face.",
        file=sys.stderr,
    )


def load_pyannote(model_name: str, hf_token: Optional[str], verbose: bool = False) -> Pipeline:
    """
    Load pyannote pipeline with fallback from 3.1 to community-1 if needed.
    Returns: Pipeline instance or exits on failure.
    """
    if verbose:
        print(f"INFO: Loading pyannote model: {model_name}", file=sys.stderr)

    def from_pretrained_with_compatible_token(name: str) -> Pipeline:
        """
        Support both legacy and newer pyannote/huggingface-hub auth argument names.
        """
        if not hf_token:
            return Pipeline.from_pretrained(name)

        try:
            params = inspect.signature(Pipeline.from_pretrained).parameters
        except (TypeError, ValueError):
            params = {}

        if "token" in params:
            return Pipeline.from_pretrained(name, token=hf_token)
        if "use_auth_token" in params:
            return Pipeline.from_pretrained(name, use_auth_token=hf_token)

        # Last-resort runtime probing for unusual versions/signatures.
        try:
            return Pipeline.from_pretrained(name, token=hf_token)
        except TypeError:
            return Pipeline.from_pretrained(name, use_auth_token=hf_token)

    try:
        pipeline = from_pretrained_with_compatible_token(model_name)
        if verbose:
            print(f"INFO: Successfully loaded {model_name}", file=sys.stderr)
        return pipeline
    except Exception as e:
        if verbose:
            print(f"WARNING: Failed to load {model_name}: {e}", file=sys.stderr)

        # Auto-fallback to community model
        if "speaker-diarization-3.1" in model_name:
            fallback = "pyannote/speaker-diarization-community-1"
            if verbose:
                print(f"INFO: Retrying with fallback model: {fallback}", file=sys.stderr)
            try:
                pipeline = from_pretrained_with_compatible_token(fallback)
                if verbose:
                    print(f"INFO: Successfully loaded {fallback}", file=sys.stderr)
                return pipeline
            except Exception as e2:
                print(
                    f"ERROR: Failed to load both {model_name} and {fallback}: {e2}", file=sys.stderr
                )
                print_pyannote_access_help()
                sys.exit(1)

        print(f"ERROR: Failed to load pyannote model {model_name}: {e}", file=sys.stderr)
        print_pyannote_access_help()
        sys.exit(1)


def run_diarization(
    pipeline: Pipeline,
    audio_path: Path,
    num_speakers: Optional[int],
    min_speakers: Optional[int],
    max_speakers: Optional[int],
    verbose: bool = False,
    progress: Optional[ProgressReporter] = None,
) -> Annotation:
    """Run pyannote diarization on audio file. Returns Annotation."""
    kwargs: Dict[str, int] = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    if verbose:
        print(f"INFO: Running diarization on {audio_path} with params {kwargs}", file=sys.stderr)

    if progress:
        params = ", ".join(f"{key}={value}" for key, value in kwargs.items()) or "auto speakers"
        progress.start("pyannote diarization", detail=params)

    hook = PyannoteProgressHook(progress) if progress else None
    if hook:
        with hook:
            diarization_output = pipeline(str(audio_path), hook=hook, **kwargs)
    else:
        diarization_output = pipeline(str(audio_path), **kwargs)

    if progress:
        progress.finish("pyannote diarization")

    # pyannote versions may return either Annotation directly or a DiarizeOutput
    # wrapper containing `.speaker_diarization`.
    if isinstance(diarization_output, Annotation):
        return diarization_output

    if hasattr(diarization_output, "speaker_diarization"):
        speaker_diarization = getattr(diarization_output, "speaker_diarization")
        if isinstance(speaker_diarization, Annotation):
            if verbose:
                print(
                    "INFO: Received DiarizeOutput; using .speaker_diarization field.",
                    file=sys.stderr,
                )
            return speaker_diarization

    print(
        f"ERROR: Unsupported diarization output type: {type(diarization_output).__name__}",
        file=sys.stderr,
    )
    print(
        "Expected pyannote.core.Annotation or object with .speaker_diarization.",
        file=sys.stderr,
    )
    sys.exit(1)


# ───────────────────────────────────────────────────────────────────────────────
# Merge ASR + Diarization
# ───────────────────────────────────────────────────────────────────────────────


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Compute positive overlap duration between [a0,a1] and [b0,b1]."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def merge_asr_with_diar(
    segments: List[Dict[str, Any]],
    diarization: Annotation,
    verbose: bool = False,
) -> List[TranscriptSegment]:
    """
    Merge ASR segments with diarization by assigning each ASR segment to best-matching speaker,
    normalizing speaker labels by order of first appearance.
    """
    # Build index of timed segments
    timed_segs = []
    for seg in segments:
        start = _seg_start(seg)
        end = _seg_end(seg)
        if start is not None and end is not None:
            timed_segs.append((start, end, _seg_text(seg)))

    if not timed_segs:
        if verbose:
            print(
                "WARNING: No ASR segments have timestamps; cannot merge with diarization.",
                file=sys.stderr,
            )
        return []

    if verbose:
        print(
            f"INFO: Merging {len(timed_segs)} timed ASR segments with diarization", file=sys.stderr
        )

    # Assign each ASR segment to the speaker with maximum overlap
    seg_speakers = []
    for s_start, s_end, s_text in timed_segs:
        best_speaker = None
        best_overlap = 0.0

        for turn, _, speaker_label in diarization.itertracks(yield_label=True):
            ds, de = turn.start, turn.end
            ovlp = overlap(ds, de, s_start, s_end)
            if ovlp > best_overlap:
                best_overlap = ovlp
                best_speaker = speaker_label

        seg_speakers.append((s_start, s_end, s_text, best_speaker))

    # Normalize speaker labels: re-map to sequential SPEAKER_00, SPEAKER_01, etc.
    # based on order of first appearance
    unique_speakers = []
    speaker_map = {}
    for _, _, _, speaker in seg_speakers:
        if speaker and speaker not in speaker_map:
            speaker_map[speaker] = f"SPEAKER_{len(unique_speakers):02d}"
            unique_speakers.append(speaker)

    if verbose:
        print(
            f"INFO: Normalized {len(unique_speakers)} unique speakers: {list(speaker_map.values())}",
            file=sys.stderr,
        )

    return [
        TranscriptSegment(
            start=start,
            end=end,
            text=text,
            speaker=speaker_map.get(speaker) if speaker else None,
        )
        for start, end, text, speaker in seg_speakers
    ]


# ───────────────────────────────────────────────────────────────────────────────
# Fallback: plain ASR transcript (no diarization)
# ───────────────────────────────────────────────────────────────────────────────


def plain_transcript(segments: List[Dict[str, Any]], fallback_text: Optional[str]) -> str:
    """Produce plain transcript from ASR segments or fallback text."""
    if fallback_text:
        return fallback_text.strip()
    texts = [_seg_text(s) for s in segments]
    return " ".join(t for t in texts if t).strip()


def transcript_segments_from_whisper(
    segments: List[Dict[str, Any]],
    fallback_text: Optional[str],
) -> List[TranscriptSegment]:
    """Convert normalized whisper segment dictionaries to shared transcript segments."""
    transcript_segments = []
    for seg in segments:
        text = _seg_text(seg)
        if not text:
            continue
        start = _seg_start(seg)
        end = _seg_end(seg)
        if start is None:
            start = 0.0
        if end is None:
            end = start
        if end < start:
            end = start
        speaker = seg.get("speaker") if isinstance(seg, dict) else None
        transcript_segments.append(
            TranscriptSegment(start=start, end=end, text=text, speaker=speaker)
        )

    if transcript_segments:
        return transcript_segments

    if fallback_text:
        return [TranscriptSegment(start=0.0, end=0.0, text=fallback_text.strip())]

    return []


# ───────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ───────────────────────────────────────────────────────────────────────────────


def run_ffmpeg_convert(
    input_path: Path,
    output_path: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    verbose: bool,
    progress: Optional[ProgressReporter] = None,
) -> None:
    """Convert input media to mono 16 kHz WAV using ffmpeg."""
    duration = probe_media_duration(input_path, ffprobe_bin, verbose) if progress else None

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
            str(output_path),
        ]
    )

    if verbose:
        print(f"INFO: Running ffmpeg: {' '.join(cmd)}", file=sys.stderr)

    if progress:
        detail = f"duration {format_duration(duration)}" if duration else "duration unknown"
        progress.start("ffmpeg conversion", detail=detail)
        output_tail: List[str] = []
        saw_ffmpeg_progress = False

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print(f"ERROR: ffmpeg binary not found: {ffmpeg_bin}", file=sys.stderr)
            sys.exit(1)

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            if "=" not in line:
                output_tail.append(line)
                if verbose:
                    print(line, file=sys.stderr)
                continue

            key, value = line.split("=", 1)
            processed_seconds: Optional[float] = None
            if key in {"out_time_ms", "out_time_us"}:
                try:
                    processed_seconds = int(value) / 1_000_000.0
                except ValueError:
                    processed_seconds = None
            elif key == "out_time":
                processed_seconds = parse_ffmpeg_timestamp(value)
            elif key == "progress" and value == "end":
                if duration and not saw_ffmpeg_progress:
                    progress.update(
                        "ffmpeg conversion",
                        completed=duration,
                        total=duration,
                        force=True,
                        show_count=False,
                    )
                continue

            if processed_seconds is not None:
                saw_ffmpeg_progress = True
                if duration:
                    progress.update(
                        "ffmpeg conversion",
                        completed=min(processed_seconds, duration),
                        total=duration,
                        show_count=False,
                    )
                else:
                    progress.update(
                        "ffmpeg conversion",
                        completed=processed_seconds,
                    )

        returncode = process.wait()
        if returncode != 0:
            print_process_tail(output_tail, "ffmpeg")
            print(f"ERROR: ffmpeg conversion failed with exit code {returncode}", file=sys.stderr)
            sys.exit(1)

        try:
            validate_nonempty_output(output_path, "ffmpeg")
        except SystemExit:
            print_process_tail(output_tail, "ffmpeg")
            raise

        progress.finish("ffmpeg conversion", detail=str(output_path))
        return

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=None if verbose else subprocess.DEVNULL,
            stderr=None if verbose else subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(f"ERROR: ffmpeg binary not found: {ffmpeg_bin}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ffmpeg conversion failed: {e}", file=sys.stderr)
        sys.exit(1)

    validate_nonempty_output(output_path, "ffmpeg")


def resolve_whisper_bin(whisper_bin: str, verbose: bool = False) -> str:
    """
    Resolve whisper executable name/path.

    If user keeps default `whisper-cpp` and that binary is unavailable, auto-fallback
    to `whisper-cli` (used by current Homebrew whisper-cpp formula).
    """
    if shutil.which(whisper_bin):
        return whisper_bin

    if whisper_bin == "whisper-cpp":
        fallback_bin = "whisper-cli"
        if shutil.which(fallback_bin):
            if verbose:
                print(
                    "INFO: 'whisper-cpp' not found; using 'whisper-cli' from PATH.",
                    file=sys.stderr,
                )
            return fallback_bin

    return whisper_bin


def run_whisper(
    audio_path: Path,
    json_path: Path,
    whisper_bin: str,
    model_path: Path,
    threads: int,
    language: str,
    verbose: bool,
    progress: Optional[ProgressReporter] = None,
) -> None:
    """Run whisper-cpp to produce JSON output."""
    cmd = [
        whisper_bin,
        "-m",
        str(model_path),
        "-f",
        str(audio_path),
        "-t",
        str(threads),
        "-oj",  # JSON output
        "-of",
        str(json_path.with_suffix("")),  # whisper-cpp adds .json
    ]
    if progress:
        cmd.append("-pp")  # print progress

    cmd.extend(["-l", language])

    if verbose:
        print(f"INFO: Running whisper-cpp: {' '.join(cmd)}", file=sys.stderr)

    if progress:
        progress.start("whisper-cpp ASR")
        output_tail: List[str] = []
        progress_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")
        last_percent: Optional[float] = None

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print(f"ERROR: whisper-cpp binary not found: {whisper_bin}", file=sys.stderr)
            sys.exit(1)

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            progress_match = progress_pattern.search(line) if "progress" in line.lower() else None
            if progress_match:
                percent = float(progress_match.group(1))
                last_percent = percent
                progress.update(
                    "whisper-cpp ASR",
                    completed=percent,
                    total=100.0,
                    force=percent >= 100.0,
                    show_count=False,
                )
                continue

            output_tail.append(line)
            if verbose:
                print(line, file=sys.stderr)

        returncode = process.wait()
        if returncode != 0:
            print_process_tail(output_tail, "whisper-cpp")
            print(f"ERROR: whisper-cpp failed with exit code {returncode}", file=sys.stderr)
            sys.exit(1)

        if not json_path.exists():
            print_process_tail(output_tail, "whisper-cpp")
            print(
                f"ERROR: whisper-cpp finished without writing expected JSON: {json_path}",
                file=sys.stderr,
            )
            sys.exit(1)

        if last_percent is None or last_percent < 100.0:
            progress.update(
                "whisper-cpp ASR",
                completed=100.0,
                total=100.0,
                force=True,
                show_count=False,
            )
        progress.finish("whisper-cpp ASR", detail=str(json_path))
        return

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=None if verbose else subprocess.DEVNULL,
            stderr=None if verbose else subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(f"ERROR: whisper-cpp binary not found: {whisper_bin}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: whisper-cpp failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not json_path.exists():
        print(f"ERROR: whisper-cpp did not create expected JSON: {json_path}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robust whisper-cpp transcription with optional pyannote diarization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", type=Path, help="Input media file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output transcript path (default: based on input and --format)",
    )
    parser.add_argument(
        "--format",
        choices=["txt", "srt", "vtt", "diarized-txt"],
        help="Output format (default: txt, or diarized-txt with --diarization)",
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="Path to ffmpeg binary")
    parser.add_argument("--ffprobe-bin", default="ffprobe", help="Path to ffprobe binary")
    parser.add_argument("--whisper-bin", default="whisper-cpp", help="Path to whisper-cpp binary")
    parser.add_argument(
        "--large-model",
        type=Path,
        default=Path.home() / "models" / "ggml-large-v3-turbo-q8_0.bin",
        help="Path to whisper large model",
    )
    parser.add_argument(
        "--threads", type=int, default=os.cpu_count(), help="Number of threads for whisper-cpp"
    )
    parser.add_argument("--language", default="auto", help="Language code (default: auto)")
    parser.add_argument("--no-ffmpeg", action="store_true", help="Skip ffmpeg pre-conversion")
    parser.add_argument(
        "--diarization",
        action="store_true",
        help="Run pyannote diarization and write speaker-labeled output",
    )
    parser.add_argument(
        "--no-diarization",
        action="store_false",
        dest="diarization",
        help="Skip pyannote diarization and write a plain ASR transcript (default)",
    )
    parser.add_argument("--hf-token", help="HuggingFace token (or set HF_TOKEN env)")
    parser.add_argument(
        "--pyannote-model",
        default="pyannote/speaker-diarization-3.1",
        help="Pyannote diarization model (used with --diarization)",
    )
    parser.add_argument("--num-speakers", type=int, help="Number of speakers (exact)")
    parser.add_argument("--min-speakers", type=int, help="Minimum number of speakers")
    parser.add_argument("--max-speakers", type=int, help="Maximum number of speakers")
    parser.add_argument(
        "--style",
        choices=["labels", "breaks"],
        default="labels",
        help="Transcript style: labels (SPEAKER_XX:) or breaks (--- speaker change ---)",
    )
    parser.add_argument(
        "--speakers", help="Comma-separated speaker names (maps to SPEAKER_00, SPEAKER_01, ...)"
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary files")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress/ETA reports")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=10.0,
        help="Seconds between progress reports (default: 10)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Setup
    maybe_set_metal_env()
    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    speaker_names = [s.strip() for s in args.speakers.split(",")] if args.speakers else None
    args.whisper_bin = resolve_whisper_bin(args.whisper_bin, args.verbose)
    progress = None if args.no_progress else ProgressReporter(interval=args.progress_interval)
    output_format = args.format or ("diarized-txt" if args.diarization else "txt")

    if output_format == "diarized-txt" and not args.diarization:
        print("ERROR: --format diarized-txt requires --diarization.", file=sys.stderr)
        sys.exit(1)

    if not args.input.exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not args.large_model.exists():
        print(f"ERROR: Whisper model not found: {args.large_model}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    elif output_format == "diarized-txt":
        output_path = args.input.with_suffix(".spk.txt")
    else:
        output_path = args.input.with_suffix(f".{output_format}")

    # Temporary files
    temp_dir = tempfile.mkdtemp(prefix="whisper_pyannote_")
    temp_dir_path = Path(temp_dir)
    wav_path = temp_dir_path / "audio.wav"
    json_path = temp_dir_path / "whisper.json"

    try:
        # Step 1: Convert audio (or use original)
        if args.no_ffmpeg:
            audio_for_processing = args.input
            if progress:
                progress.info(f"Skipping ffmpeg conversion; using original file: {args.input}")
            elif args.verbose:
                print(
                    f"INFO: Skipping ffmpeg conversion; using original file: {args.input}",
                    file=sys.stderr,
                )
        else:
            run_ffmpeg_convert(
                args.input,
                wav_path,
                args.ffmpeg_bin,
                args.ffprobe_bin,
                args.verbose,
                progress,
            )
            audio_for_processing = wav_path

        # Step 2: Run whisper-cpp
        run_whisper(
            audio_for_processing,
            json_path,
            args.whisper_bin,
            args.large_model,
            args.threads,
            args.language,
            args.verbose,
            progress,
        )

        # Step 3: Load ASR results
        if progress:
            progress.start("loading whisper output")
        segments, fallback_text = load_whisper_segments(json_path, args.verbose)
        if progress:
            progress.finish("loading whisper output")
        transcript_segments = transcript_segments_from_whisper(segments, fallback_text)

        # Check if segments have timestamps
        has_timestamps = any(_has_time(s) for s in segments)
        if not has_timestamps and not fallback_text:
            print("ERROR: ASR produced no usable segments.", file=sys.stderr)
            sys.exit(1)

        # Step 4: Run diarization
        if not args.diarization:
            if progress:
                progress.info(
                    "Skipping pyannote diarization (default; pass --diarization to enable)"
                )
        else:
            if progress:
                progress.start("pyannote model load", detail=args.pyannote_model)
            pipeline = load_pyannote(args.pyannote_model, hf_token, args.verbose)
            if progress:
                progress.finish("pyannote model load")

            diarization = run_diarization(
                pipeline,
                audio_for_processing,
                args.num_speakers,
                args.min_speakers,
                args.max_speakers,
                args.verbose,
                progress,
            )

            # Step 5: Merge
            if has_timestamps:
                if progress:
                    progress.start("merging ASR with diarization")
                diarized_segments = merge_asr_with_diar(segments, diarization, args.verbose)
                if diarized_segments:
                    transcript_segments = remap_speakers(diarized_segments, speaker_names)
                if progress:
                    progress.finish("merging ASR with diarization")
            else:
                if args.verbose:
                    print(
                        "WARNING: No timestamps in ASR segments; skipping diarization merge.",
                        file=sys.stderr,
                    )

        # Step 6: Write output
        if not transcript_segments:
            if args.verbose:
                print(
                    "WARNING: No transcript segments; falling back to plain ASR transcript.",
                    file=sys.stderr,
                )
            transcript_segments = [
                TranscriptSegment(0.0, 0.0, plain_transcript(segments, fallback_text))
            ]

        final_text = emit_transcript(transcript_segments, output_format, speaker_names)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_text)
            f.write("\n")

        print(f"✓ Transcript written to: {output_path}")

    finally:
        if not args.keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)
        elif args.verbose:
            print(f"INFO: Temporary files kept in: {temp_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
