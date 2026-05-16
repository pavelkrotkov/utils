#!/usr/bin/env python3
# /// script
# dependencies = [
#   "mlx-audio>=0.4.3,<0.5; platform_system == 'Darwin' and platform_machine == 'arm64'",
# ]
# ///
"""
Transcribe audio locally with VibeVoice-ASR through mlx-audio.

Defaults to mlx-community/VibeVoice-ASR-4bit and writes native mlx-audio JSON,
including speaker IDs and timestamps when the model returns them.

Usage:
    uv run ./audio_transcribe_vibevoice.py interview.m4a
    uv run ./audio_transcribe_vibevoice.py interview.m4a --context "Pavel, Mathpix, pyannote"
    uv run ./audio_transcribe_vibevoice.py interview.m4a --format txt -o interview.txt
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path


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
        "ERROR: mlx-audio/VibeVoice-ASR is intended for Apple Silicon Macs "
        "(Darwin arm64).",
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
        generated_path = final_path
    else:
        mlx_stem = final_path.with_name(f"{final_path.name}.mlx-audio")
        generated_path = Path(f"{mlx_stem}.{output_format}")

    return final_path, mlx_stem, generated_path


def validate_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        print(f"ERROR: mlx-audio did not create a non-empty output file: {path}", file=sys.stderr)
        sys.exit(1)


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
            format=args.format,
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

    if generated_path != final_path:
        generated_path.replace(final_path)

    validate_output(final_path)
    print(f"Transcript written to: {final_path}")


if __name__ == "__main__":
    main()
