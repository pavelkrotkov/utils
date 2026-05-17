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
emits txt, srt, or vtt locally.

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
from pathlib import Path
from typing import Any

from audio_transcript import TranscriptSegment, emit_transcript


DEFAULT_MODEL = "mlx-community/VibeVoice-ASR-4bit"
SUPPORTED_FORMATS = ("json", "txt", "srt", "vtt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe audio locally with VibeVoice-ASR via mlx-audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", type=Path, help="Input audio/media file")
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
    if not path.exists() or path.stat().st_size == 0:
        print(f"ERROR: mlx-audio did not create a non-empty output file: {path}", file=sys.stderr)
        sys.exit(1)


def load_vibevoice_segments(json_path: Path) -> list[TranscriptSegment]:
    with open(json_path, "r", encoding="utf-8") as file:
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
        return data
    if not isinstance(data, dict):
        return []

    for key in ("segments", "chunks", "transcription", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_raw_segments(value)
            if nested:
                return nested

    return []


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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

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

    print(f"INFO: Transcribing with {args.model}", file=sys.stderr)
    print(f"INFO: Writing {args.format.upper()} to {final_path}", file=sys.stderr)

    try:
        generate_transcription(
            model=args.model,
            audio=str(args.input),
            output_path=str(mlx_stem),
            format="json",
            verbose=args.verbose,
            context=args.context,
        )
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
        segments = load_vibevoice_segments(generated_path)
        final_path.write_text(emit_transcript(segments, args.format) + "\n", encoding="utf-8")

    validate_output(final_path)
    print(f"Transcript written to: {final_path}")


if __name__ == "__main__":
    main()
