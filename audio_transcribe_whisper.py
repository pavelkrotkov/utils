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
  ./audio_transcribe_whisper.py input.m4a --max-context -1
  ./audio_transcribe_whisper.py input.m4a --diarization --speakers "Alice,Bob" --num-speakers 2
  ./audio_transcribe_whisper.py input.m4a --diarization --style breaks
  ./audio_transcribe_whisper.py input.m4a --diarization --no-ffmpeg --pyannote-model pyannote/speaker-diarization-community-1

By default, this wrapper passes `-mc 0` to whisper-cpp to avoid rolling-context
hallucination loops on dictation or meeting audio with long pauses. Use
`--max-context -1` to restore whisper-cpp's default context behavior when
continuity across decode windows is more important than hallucination risk.
"""

import argparse
import inspect
import itertools
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audio_common import (
    ProgressReporter,
    convert_to_pcm16k_mono,
    print_process_tail,
    run_with_progress,
)
from audio_segments import Transcript, TranscriptError, read_transcript_file
from audio_transcript import TranscriptSegment, emit_transcript

DEFAULT_MAX_CONTEXT = 0

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
# Progress parsing
# ───────────────────────────────────────────────────────────────────────────────


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
        file: dict[str, Any] | None = None,
        total: int | None = None,
        completed: int | None = None,
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


# ───────────────────────────────────────────────────────────────────────────────
# JSON robustness: whisper-cpp output parsing
# ───────────────────────────────────────────────────────────────────────────────


def load_whisper_transcript(json_path: Path, verbose: bool = False) -> Transcript:
    """Load whisper-cpp JSON as a transcript, or exit with a CLI-oriented error.

    Decoding lives in ``audio_segments``; this only adds whisper's wording for
    the ways the file can be unusable.
    """
    try:
        transcript = read_transcript_file(json_path, label="Whisper")
    except TranscriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not transcript.segments:
        if transcript.fallback_text:
            if verbose:
                print(
                    "INFO: No usable 'segments'/'transcription' field, using top-level 'text'",
                    file=sys.stderr,
                )
            return transcript
        print(
            "ERROR: Whisper JSON has no usable 'segments'/'transcription' and no 'text': "
            f"{json_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    return transcript


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


def load_pyannote(model_name: str, hf_token: str | None, verbose: bool = False) -> Any:
    """
    Load pyannote pipeline with fallback from 3.1 to community-1 if needed.
    Returns: Pipeline instance or exits on failure.
    """
    try:
        # Keep pyannote optional unless diarization is explicitly requested.
        from pyannote.audio import Pipeline
    except ImportError as e:
        print(f"ERROR: Missing required Python package: {e}", file=sys.stderr)
        print("Install with: pip install torch pyannote.audio", file=sys.stderr)
        sys.exit(1)

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
    pipeline: Any,
    audio_path: Path,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
    verbose: bool = False,
    progress: ProgressReporter | None = None,
) -> Any:
    """Run pyannote diarization on audio file. Returns Annotation."""
    try:
        from pyannote.core import Annotation
    except ImportError as e:
        print(f"ERROR: Missing required Python package: {e}", file=sys.stderr)
        print("Install with: pip install torch pyannote.audio", file=sys.stderr)
        sys.exit(1)

    kwargs: dict[str, int] = {}
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
        speaker_diarization = diarization_output.speaker_diarization
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


def merge_asr_turn_segments(
    asr_segments: list[tuple[float, float, str]],
    speaker_turns: list[tuple[float, float, str]],
    verbose: bool = False,
) -> list[TranscriptSegment]:
    """
    Merge timed ASR tuples with speaker-turn tuples and return transcript segments.

    ASR input is (start, end, text). Speaker-turn input is (start, end, label).
    Output segments carry normalized speaker labels (`SPEAKER_NN` by order of
    first appearance), which `emit_diarized_txt` later maps to user-supplied
    names by index.
    """
    seg_speakers: list[tuple[float, float, str, str | None]] = []
    for s_start, s_end, s_text in asr_segments:
        best_speaker = None
        best_overlap = 0.0

        for d_start, d_end, speaker_label in speaker_turns:
            ovlp = overlap(d_start, d_end, s_start, s_end)
            if ovlp > best_overlap:
                best_overlap = ovlp
                best_speaker = speaker_label

        seg_speakers.append((s_start, s_end, s_text, best_speaker))

    speaker_map: dict[str, str] = {}
    for _, _, _, speaker in seg_speakers:
        if speaker and speaker not in speaker_map:
            speaker_map[speaker] = f"SPEAKER_{len(speaker_map):02d}"

    if verbose:
        print(
            f"INFO: Normalized {len(speaker_map)} unique speakers: {list(speaker_map.values())}",
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


def merge_asr_turns(
    asr_segments: list[tuple[float, float, str]],
    speaker_turns: list[tuple[float, float, str]],
    style: str,
    speaker_names: list[str] | None,
    verbose: bool = False,
) -> list[str]:
    """Merge timed ASR and speaker-turn tuples into legacy transcript lines."""
    segments = merge_asr_turn_segments(asr_segments, speaker_turns, verbose)
    if style == "labels":
        return emit_transcript(segments, "diarized-txt", speaker_names).splitlines()

    lines: list[str] = []
    for _, group in itertools.groupby(segments, key=lambda segment: segment.speaker):
        texts = [segment.text for segment in group if segment.text]
        combined = " ".join(texts).strip()
        if combined:
            lines.append("--- speaker change ---")
            lines.append(combined)
    return lines


def merge_asr_with_diar(
    segments: Sequence[TranscriptSegment],
    diarization: Any,
    verbose: bool = False,
) -> list[TranscriptSegment]:
    """
    Merge ASR segments with diarization by assigning each ASR segment to best-matching speaker.

    Returns segments tagged with normalized `SPEAKER_NN` labels (by order of
    first appearance); user-supplied names are applied later at emit time.
    """
    timed_segs = [(seg.start, seg.end, seg.text) for seg in segments]

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

    speaker_turns = [
        (turn.start, turn.end, speaker_label)
        for turn, _, speaker_label in diarization.itertracks(yield_label=True)
    ]
    return merge_asr_turn_segments(timed_segs, speaker_turns, verbose)


# ───────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ───────────────────────────────────────────────────────────────────────────────


def run_ffmpeg_convert(
    input_path: Path,
    output_path: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    verbose: bool,
    progress: ProgressReporter | None = None,
) -> None:
    """Convert input media to mono 16 kHz WAV using ffmpeg."""
    convert_to_pcm16k_mono(
        input_path,
        output_path,
        progress=progress,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        verbose=verbose,
    )


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


def parse_max_context(value: str) -> int:
    """Argparse type for whisper-cpp max-context values."""
    try:
        max_context = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be -1 or a non-negative integer") from None
    if max_context < -1:
        raise argparse.ArgumentTypeError("must be -1 or a non-negative integer")
    return max_context


def run_whisper(
    audio_path: Path,
    json_path: Path,
    whisper_bin: str,
    model_path: Path,
    threads: int,
    language: str,
    verbose: bool,
    progress: ProgressReporter | None = None,
    max_context: int = DEFAULT_MAX_CONTEXT,
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
    if max_context != -1:
        cmd.extend(["-mc", str(max_context)])

    if verbose:
        print(f"INFO: Running whisper-cpp: {' '.join(cmd)}", file=sys.stderr)

    progress_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")

    def parse_progress(line: str) -> float | None:
        progress_match = progress_pattern.search(line) if "progress" in line.lower() else None
        if not progress_match:
            return None
        return float(progress_match.group(1))

    output_tail = run_with_progress(
        cmd,
        "whisper-cpp ASR",
        parse_progress,
        reporter=progress,
        verbose=verbose,
        finish_detail=str(json_path) if progress else None,
        missing_binary_label=whisper_bin,
        force_final_percent=True,
    )

    if not json_path.exists():
        print_process_tail(output_tail, "whisper-cpp ASR")
        print(
            f"ERROR: whisper-cpp finished without writing expected JSON: {json_path}",
            file=sys.stderr,
        )
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
        choices=["txt", "srt", "vtt", "diarized-txt", "diarized-breaks"],
        help=(
            "Output format (default: txt, or diarized-txt with --diarization; "
            "--style breaks is shorthand for --format diarized-breaks)"
        ),
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
    parser.add_argument(
        "--max-context",
        type=parse_max_context,
        default=DEFAULT_MAX_CONTEXT,
        help=(
            "Maximum text context tokens to carry between decode windows "
            f"(default: {DEFAULT_MAX_CONTEXT} to reduce hallucination loops; "
            "use -1 for whisper-cpp default)"
        ),
    )
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
    diarized_default = "diarized-breaks" if args.style == "breaks" else "diarized-txt"
    output_format = args.format or (
        diarized_default if (args.diarization or args.style == "breaks") else "txt"
    )

    if output_format in {"diarized-txt", "diarized-breaks"} and not args.diarization:
        print(f"ERROR: --format {output_format} requires --diarization.", file=sys.stderr)
        sys.exit(1)

    if not args.input.exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not args.large_model.exists():
        print(f"ERROR: Whisper model not found: {args.large_model}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    elif output_format in {"diarized-txt", "diarized-breaks"}:
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
            max_context=args.max_context,
        )

        # Step 3: Load ASR results
        if progress:
            progress.start("loading whisper output")
        transcript = load_whisper_transcript(json_path, args.verbose)
        if progress:
            progress.finish("loading whisper output")
        transcript_segments = transcript.resolved_segments()

        has_timestamps = transcript.has_timing
        if not has_timestamps and not transcript.fallback_text:
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
                diarized_segments = merge_asr_with_diar(
                    transcript.segments, diarization, args.verbose
                )
                if diarized_segments:
                    transcript_segments = diarized_segments
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
            transcript_segments = [TranscriptSegment(0.0, 0.0, transcript.plain_text())]

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
