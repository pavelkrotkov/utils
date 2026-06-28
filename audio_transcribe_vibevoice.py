#!/usr/bin/env python3
# /// script
# dependencies = [
#   "mlx-audio>=0.4.3,<0.5; platform_system == 'Darwin' and platform_machine == 'arm64'",
# ]
# ///
"""
Transcribe audio locally with VibeVoice-ASR through mlx-audio.

Defaults to mlx-community/VibeVoice-ASR-4bit. mlx-audio is asked for raw JSON,
then this script normalizes that JSON into shared TranscriptSegment objects and
emits json, txt, srt, or vtt locally.

Usage:
    uv run ./audio_transcribe_vibevoice.py interview.m4a
    uv run ./audio_transcribe_vibevoice.py interview.m4a --context "Pavel, Mathpix, pyannote"
    uv run ./audio_transcribe_vibevoice.py interview.m4a --format srt -o interview.srt
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

from audio_common import (
    ProgressReporter,
    convert_to_pcm16k_mono,
    run_threaded_with_periodic_progress,
)
from audio_transcript import TranscriptSegment, emit_transcript

DEFAULT_MODEL = "mlx-community/VibeVoice-ASR-4bit"
SUPPORTED_FORMATS = ("json", "txt", "srt", "vtt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe audio locally with VibeVoice-ASR via mlx-audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        default=None,
        help="Input audio/media file (required unless --from-json is used)",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        metavar="JSON",
        help="Convert an existing VibeVoice JSON to another format without re-transcribing",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path (default: <input>.vibevoice.<format>)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face model repo or local path (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--format",
        choices=SUPPORTED_FORMATS,
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--context",
        help="Optional hotwords or domain context to guide transcription",
    )
    parser.add_argument(
        "--pre-convert-pcm16k",
        action="store_true",
        help=(
            "Convert input to mono 16 kHz WAV before VibeVoice "
            "(also enabled by VIBEVOICE_PRECONVERT_PCM16K=1)"
        ),
    )
    parser.add_argument("--no-progress", action="store_true", help="Disable progress reports")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=10.0,
        help="Seconds between progress reports (default: 10)",
    )
    parser.add_argument("--verbose", action="store_true", help="Show mlx-audio details")
    return parser


def ensure_apple_silicon() -> None:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return

    print(
        "ERROR: mlx-audio/VibeVoice-ASR is intended for Apple Silicon Macs (Darwin arm64).",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_output_paths(
    input_path: Path,
    output_path: Path | None,
    output_format: str,
) -> tuple[Path, Path, Path]:
    if output_path is None:
        final_path = input_path.with_name(f"{input_path.stem}.vibevoice.{output_format}")
    else:
        final_path = output_path

    final_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{output_format}"
    if final_path.name.lower().endswith(suffix):
        mlx_stem = Path(str(final_path)[: -len(suffix)])
    else:
        mlx_stem = final_path.with_name(f"{final_path.name}.mlx-audio")

    return final_path, mlx_stem, Path(f"{mlx_stem}.json")


def validate_output(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        print(f"ERROR: mlx-audio did not create a non-empty output file: {path}", file=sys.stderr)
        sys.exit(1)


def load_vibevoice_segments(json_path: Path) -> list[TranscriptSegment]:
    with open(json_path, encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Invalid VibeVoice JSON in {json_path}: {exc}", file=sys.stderr)
            sys.exit(1)

    raw_segments = _extract_raw_segments(data)
    segments = [_segment_from_raw(item) for item in raw_segments]
    segments = [segment for segment in segments if segment.text.strip()]

    if segments:
        return segments

    fallback_text = _extract_text(data)
    if fallback_text:
        return [TranscriptSegment(start=0.0, end=0.0, text=fallback_text)]

    print(f"ERROR: VibeVoice JSON contained no transcript text: {json_path}", file=sys.stderr)
    sys.exit(1)


def _extract_raw_segments(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data if _looks_like_segment_list(data) else []
    if not isinstance(data, dict):
        return []

    for key in ("segments", "chunks", "transcription", "results"):
        value = data.get(key)
        if isinstance(value, list):
            if _looks_like_segment_list(value):
                return value
            continue
        if isinstance(value, dict):
            nested = _extract_raw_segments(value)
            if nested:
                return nested

    return []


def _looks_like_segment_list(value: list[Any]) -> bool:
    return any(isinstance(item, str) or _looks_like_segment(item) for item in value)


def _looks_like_segment(value: Any) -> bool:
    if not isinstance(value, dict):
        return False

    segment_keys = {
        "text",
        "content",
        "utterance",
        "transcript",
        "start",
        "start_time",
        "begin",
        "ts",
        "t0",
        "end",
        "end_time",
        "finish",
        "te",
        "t1",
        "offsets",
        "speaker",
        "speaker_id",
        "speaker_label",
    }
    return any(key in value for key in segment_keys)


def _segment_from_raw(raw: Any) -> TranscriptSegment:
    if isinstance(raw, str):
        return TranscriptSegment(start=0.0, end=0.0, text=raw)

    if not isinstance(raw, dict):
        return TranscriptSegment(start=0.0, end=0.0, text=str(raw))

    text = _extract_text(raw)
    start = _extract_time(raw, "start", "start_time", "begin", "ts", "t0")
    end = _extract_time(raw, "end", "end_time", "finish", "te", "t1")
    offsets = raw.get("offsets")
    if isinstance(offsets, dict):
        if start is None and "from" in offsets:
            start = _coerce_seconds(offsets["from"], milliseconds=True)
        if end is None and "to" in offsets:
            end = _coerce_seconds(offsets["to"], milliseconds=True)

    start = 0.0 if start is None else start
    end = start if end is None or end < start else end
    speaker = raw.get("speaker") or raw.get("speaker_id") or raw.get("speaker_label")
    return TranscriptSegment(
        start=start, end=end, text=text, speaker=str(speaker) if speaker else None
    )


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("text", "content", "utterance", "transcript"):
        if key in value:
            return str(value[key]).strip()
    return ""


def _extract_time(value: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in value:
            return _coerce_seconds(value[key], centiseconds=key in {"t0", "t1"})
    return None


def _coerce_seconds(
    value: Any,
    *,
    centiseconds: bool = False,
    milliseconds: bool = False,
) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if centiseconds:
        return seconds * 0.01
    if milliseconds:
        return seconds * 0.001
    return seconds


def _bool_env(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.from_json and args.input:
        parser.error("--from-json and input are mutually exclusive")
    if not args.from_json and not args.input:
        parser.error("input is required unless --from-json is used")

    if args.from_json:
        if args.format == "json":
            parser.error("--format must be txt, srt, or vtt when using --from-json")
        if not args.from_json.exists() or not args.from_json.is_file():
            print(f"ERROR: JSON file not found: {args.from_json}", file=sys.stderr)
            sys.exit(1)

        out_path = args.output if args.output else args.from_json.with_suffix(f".{args.format}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        segments = load_vibevoice_segments(args.from_json)
        out_path.write_text(emit_transcript(segments, args.format) + "\n", encoding="utf-8")
        print(f"Transcript written to: {out_path}")
        return

    ensure_apple_silicon()

    if not args.input.exists() or not args.input.is_file():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    try:
        from mlx_audio.stt.generate import generate_transcription
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Run with: uv run ./audio_transcribe_vibevoice.py ...", file=sys.stderr)
        sys.exit(1)

    final_path, mlx_stem, generated_path = resolve_output_paths(
        args.input,
        args.output,
        args.format,
    )
    old_mtime_ns = generated_path.stat().st_mtime_ns if generated_path.exists() else None

    progress = None if args.no_progress else ProgressReporter(interval=args.progress_interval)
    pre_convert = args.pre_convert_pcm16k or _bool_env("VIBEVOICE_PRECONVERT_PCM16K")
    if progress:
        progress.info(f"Transcribing with {args.model}")
        progress.info(f"Writing {args.format.upper()} to {final_path}")
    else:
        print(f"INFO: Transcribing with {args.model}", file=sys.stderr)
        print(f"INFO: Writing {args.format.upper()} to {final_path}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="vibevoice_pcm16k_") as temp_dir:
        audio_for_transcription = args.input
        if pre_convert:
            audio_for_transcription = Path(temp_dir) / "audio.wav"
            convert_to_pcm16k_mono(
                args.input,
                audio_for_transcription,
                progress=progress,
                verbose=args.verbose,
            )

        def transcribe() -> None:
            # Always ask mlx-audio for JSON; we emit the user's requested
            # format locally via emit_transcript so all backends share the
            # same txt/srt/vtt output policy.
            generate_transcription(
                model=args.model,
                audio=str(audio_for_transcription),
                output_path=str(mlx_stem),
                format="json",
                verbose=args.verbose,
                context=args.context,
            )

        try:
            if progress:
                run_threaded_with_periodic_progress(
                    transcribe,
                    reporter=progress,
                    label="VibeVoice ASR",
                    interval=args.progress_interval,
                )
            else:
                transcribe()
        except Exception as exc:
            print(f"ERROR: VibeVoice transcription failed: {exc}", file=sys.stderr)
            sys.exit(1)

    validate_output(generated_path)
    if old_mtime_ns is not None and generated_path.stat().st_mtime_ns == old_mtime_ns:
        print(f"ERROR: mlx-audio did not update output file: {generated_path}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        if generated_path != final_path:
            generated_path.replace(final_path)
    else:
        try:
            segments = load_vibevoice_segments(generated_path)
            final_path.write_text(emit_transcript(segments, args.format) + "\n", encoding="utf-8")
        finally:
            generated_path.unlink(missing_ok=True)

    validate_output(final_path)
    print(f"Transcript written to: {final_path}")


if __name__ == "__main__":
    main()
