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

Long files on memory-constrained Macs (16 GB can OOM during prefill):
    uv run ./audio_transcribe_vibevoice.py long_interview.m4a --chunk-seconds 300

Chunking splits the input near natural silences into pieces of at most
--chunk-seconds, transcribes each through the same single-pass model call, and
splices the per-chunk segments back onto one absolute timeline. Windows with no
usable silence are hard-cut with a small overlap whose double-transcribed span
is deduped at splice time. Known limitation: speaker labels reset at chunk
boundaries ("Speaker 1" in chunk 2 is not necessarily "Speaker 1" in chunk 1).
Chunking trades VibeVoice's cross-file speaker consistency for the ability to
run at all on low memory; use it when you need *what* was said, not *who*.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_common import (
    ProgressReporter,
    convert_to_pcm16k_mono,
    format_duration,
    probe_media_duration,
    run_threaded_with_periodic_progress,
)
from audio_transcript import TranscriptSegment, emit_transcript

DEFAULT_MODEL = "mlx-community/VibeVoice-ASR-4bit"
# Observed on Apple Silicon: transcription takes roughly 4x the audio duration.
REALTIME_FACTOR = 4.0
SUPPORTED_FORMATS = ("json", "txt", "srt", "vtt", "diarized-txt", "diarized-breaks")
_DIARIZED_FORMATS = frozenset({"diarized-txt", "diarized-breaks"})
DEFAULT_CHUNK_SECONDS = 300.0
DEFAULT_SILENCE_DB = -30.0
DEFAULT_SILENCE_MIN = 0.5
DEFAULT_CHUNK_OVERLAP = 2.5


@dataclass
class Chunk:
    """A half-open [start, end) piece of the input, in seconds."""

    start: float
    end: float
    # True when the boundary had no usable silence, so this chunk rewound by
    # --chunk-overlap and its double-transcribed span must be deduped at splice.
    overlaps_previous: bool = False


def _format_file_ext(output_format: str) -> str:
    return "txt" if output_format in _DIARIZED_FORMATS else output_format


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
            "(also enabled by VIBEVOICE_PRECONVERT_PCM16K=1); "
            "chunked runs always convert as part of extraction"
        ),
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=0.0,
        metavar="N",
        help=(
            "Split the input into pieces of at most N seconds, transcribe each, "
            "and splice results with corrected absolute timestamps (0 = single "
            "pass, the default; single pass keeps VibeVoice's global speaker "
            "consistency). Use for long files on low-memory Macs. Boundaries "
            "snap to silence when available; speaker labels reset per chunk."
        ),
    )
    parser.add_argument(
        "--silence-db",
        type=float,
        default=DEFAULT_SILENCE_DB,
        help="Silence threshold in dB for chunk boundary detection (default: -30)",
    )
    parser.add_argument(
        "--silence-min",
        type=float,
        default=DEFAULT_SILENCE_MIN,
        help="Minimum silence duration in seconds for a chunk boundary (default: 0.5)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=float,
        default=DEFAULT_CHUNK_OVERLAP,
        help=(
            "Overlap in seconds for hard-cut boundaries with no usable silence "
            "(default: 2.5); the double-transcribed span is deduped at splice"
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
    ext = _format_file_ext(output_format)
    if output_path is None:
        final_path = input_path.with_name(f"{input_path.stem}.vibevoice.{ext}")
    else:
        final_path = output_path

    final_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{ext}"
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

    # Older mlx-audio versions serialized the segment array as a JSON string in "text".
    # The string may be truncated mid-object; recover complete segments up to last '}'.
    for key in ("text", "content"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        for candidate in _json_array_candidates(value):
            if isinstance(candidate, list) and _looks_like_segment_list(candidate):
                return candidate

    return []


def _json_array_candidates(s: str) -> list[Any]:
    """Yield parseable JSON array candidates, including a truncation-recovery fallback."""
    if not s.lstrip().startswith("["):
        return []
    try:
        parsed = json.loads(s)
        return [parsed]
    except (json.JSONDecodeError, ValueError):
        pass
    pos = len(s)
    while (pos := s.rfind("}", 0, pos)) >= 0:
        try:
            return [json.loads(s[: pos + 1] + "]")]
        except (json.JSONDecodeError, ValueError):
            pass
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
    value_keys_lower = {k.lower() for k in value}
    return bool(value_keys_lower & segment_keys)


def _segment_from_raw(raw: Any) -> TranscriptSegment:
    if isinstance(raw, str):
        return TranscriptSegment(start=0.0, end=0.0, text=raw)

    if not isinstance(raw, dict):
        return TranscriptSegment(start=0.0, end=0.0, text=str(raw))

    # Older mlx-audio versions used capitalized keys (Start, End, Speaker, Content).
    if any(k[:1].isupper() for k in raw):
        raw = {k.lower(): v for k, v in raw.items()}

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
    speaker = None
    for k in ("speaker", "speaker_id", "speaker_label"):
        if (val := raw.get(k)) is not None:
            speaker = val
            break
    return TranscriptSegment(
        start=start, end=end, text=text, speaker=str(speaker) if speaker is not None else None
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


def _warn_if_speakers_ignored(segments: list[TranscriptSegment], output_format: str) -> None:
    if output_format in _DIARIZED_FORMATS:
        return
    if any(s.speaker is not None for s in segments):
        formats_str = " or ".join(sorted(_DIARIZED_FORMATS))
        print(
            f"NOTE: transcript contains speaker labels;"
            f" use --format {formats_str} to include them.",
            file=sys.stderr,
        )


def detect_silences(
    path: Path,
    *,
    noise_db: float,
    min_silence: float,
    ffmpeg_bin: str = "ffmpeg",
) -> list[tuple[float, float]]:
    """Return (start, end) silence intervals via ffmpeg silencedetect."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        print(f"WARNING: silencedetect failed to run: {exc}", file=sys.stderr)
        return []

    silences: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in (result.stderr or "").splitlines():
        if match := re.search(r"silence_start:\s*(-?[0-9.]+)", line):
            pending_start = float(match.group(1))
            continue
        if match := re.search(r"silence_end:\s*([0-9.]+)", line):
            if pending_start is not None:
                silences.append((pending_start, float(match.group(1))))
            pending_start = None
    if pending_start is not None and (duration := probe_media_duration(path, "ffprobe")):
        # Silence ran to EOF without an explicit end event. Only record it
        # when the duration is known, so intervals always have end > start.
        silences.append((pending_start, duration))
    return silences


def plan_chunks(
    duration: float,
    chunk_seconds: float,
    silences: list[tuple[float, float]],
    overlap: float,
) -> list[Chunk]:
    """Split [0, duration] into chunks of at most ~chunk_seconds.

    Boundaries snap to the midpoint of a detected silence inside a search band
    around each target cut, so chunks butt-join cleanly with no overlap. A
    window with no usable silence hard-cuts at the target and the next chunk
    rewinds by `overlap` seconds (flagged overlaps_previous). A remainder of at
    most 25% of chunk_seconds folds into the previous chunk instead of forming
    a sliver tail.
    """
    search = chunk_seconds / 3.0
    midpoints = [(start + end) / 2.0 for start, end in silences]
    chunks: list[Chunk] = []
    start = 0.0
    overlapped = False
    while True:
        remaining = duration - start
        if remaining <= chunk_seconds * 1.25:
            chunks.append(Chunk(start=start, end=duration, overlaps_previous=overlapped))
            break
        target = start + chunk_seconds
        band = [m for m in midpoints if target - search <= m <= target + search]
        cut = min(band, key=lambda m: abs(m - target)) if band else target
        chunks.append(Chunk(start=start, end=cut, overlaps_previous=overlapped))
        if band:
            start = cut
            overlapped = False
        else:
            start = max(cut - overlap, 0.0)
            overlapped = True
    return chunks


def extract_chunk(
    input_path: Path,
    dest_wav: Path,
    chunk: Chunk,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Cut one chunk out of the input as mono 16 kHz WAV."""
    cmd = [
        ffmpeg_bin,
        "-v",
        "error",
        "-y",
        "-ss",
        f"{chunk.start:.3f}",
        "-to",
        f"{chunk.end:.3f}",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(dest_wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def merge_chunk_segments(
    chunk_segments: list[list[TranscriptSegment]],
    chunks: list[Chunk],
) -> list[TranscriptSegment]:
    """Splice per-chunk segments onto one absolute timeline.

    Each chunk's local times are shifted by its start offset. Overlapping
    (hard-cut) seams are deduped by coverage: leading segments of the later
    chunk that end at or before the latest time already covered by earlier
    chunks are dropped, so the double-transcribed span appears exactly once.
    Text is not compared — VibeVoice's rendering of an overlap region differs
    between passes, so dedupe is time-based.
    """
    merged: list[TranscriptSegment] = []
    for segments, chunk in zip(chunk_segments, chunks, strict=True):
        shifted = [
            TranscriptSegment(
                start=segment.start + chunk.start,
                end=segment.end + chunk.start,
                text=segment.text,
                speaker=segment.speaker,
            )
            for segment in segments
        ]
        if chunk.overlaps_previous and merged:
            covered_until = max(segment.end for segment in merged)
            shifted = [s for s in shifted if s.end > covered_until + 1e-6]
        merged.extend(shifted)
    merged.sort(key=lambda s: (s.start, s.end))
    return merged


def _dump_segments_json(segments: list[TranscriptSegment]) -> str:
    payload = []
    for segment in segments:
        item: dict[str, Any] = {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text,
        }
        if segment.speaker is not None:
            item["speaker"] = segment.speaker
        payload.append(item)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def run_chunked(args: argparse.Namespace, generate: Any, progress: ProgressReporter | None) -> None:
    """Chunked transcription path: split -> per-chunk ASR -> splice -> emit."""
    input_path: Path = args.input
    duration = probe_media_duration(input_path, "ffprobe", args.verbose)
    if not duration or duration <= 0:
        print("ERROR: cannot determine audio duration; cannot plan chunks", file=sys.stderr)
        sys.exit(1)

    final_path, _, generated_path = resolve_output_paths(input_path, args.output, args.format)

    if progress:
        progress.info(
            f"Audio duration {format_duration(duration)}; chunking at <= {args.chunk_seconds:g}s"
        )
    with tempfile.TemporaryDirectory(prefix="vibevoice_chunks_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        silences = detect_silences(
            input_path,
            noise_db=args.silence_db,
            min_silence=args.silence_min,
        )
        chunks = plan_chunks(duration, args.chunk_seconds, silences, args.chunk_overlap)
        plan_summary = ", ".join(
            f"{chunk.start:.1f}-{chunk.end:.1f}" + ("*" if chunk.overlaps_previous else "")
            for chunk in chunks
        )
        message = f"{len(chunks)} chunk(s) [{plan_summary}] (* = overlapped seam)"
        if progress:
            progress.info(message)
        else:
            print(f"INFO: {message}", file=sys.stderr)

        chunk_segments: list[list[TranscriptSegment]] = []
        total = len(chunks)

        def transcribe_chunk(wav_path: Path, stem: Path) -> None:
            generate(
                model=args.model,
                audio=str(wav_path),
                output_path=str(stem),
                format="json",
                verbose=args.verbose,
                context=args.context,
            )

        for index, chunk in enumerate(chunks, 1):
            wav_path = temp_dir / f"chunk_{index:03d}.wav"
            stem = temp_dir / f"chunk_{index:03d}"
            extract_chunk(input_path, wav_path, chunk)

            label = f"VibeVoice ASR chunk {index}/{total}"
            try:
                if progress:
                    run_threaded_with_periodic_progress(
                        lambda w=wav_path, s=stem: transcribe_chunk(w, s),
                        reporter=progress,
                        label=label,
                        interval=args.progress_interval,
                        expected_seconds=(chunk.end - chunk.start) * REALTIME_FACTOR,
                    )
                else:
                    transcribe_chunk(wav_path, stem)
            except Exception as exc:
                print(f"ERROR: VibeVoice transcription failed ({label}): {exc}", file=sys.stderr)
                sys.exit(1)

            generated_json = Path(f"{stem}.json")
            validate_output(generated_json)
            chunk_segments.append(load_vibevoice_segments(generated_json))

        merged = merge_chunk_segments(chunk_segments, chunks)
        if not merged:
            print("ERROR: chunked transcription produced no transcript text", file=sys.stderr)
            sys.exit(1)
        _warn_if_speakers_ignored(merged, args.format)
        if len(chunks) > 1 and any(segment.speaker is not None for segment in merged):
            print(
                "NOTE: speaker labels reset at chunk boundaries; 'Speaker N' may"
                " refer to different people in different chunks.",
                file=sys.stderr,
            )

        if args.format == "json":
            generated_path.write_text(_dump_segments_json(merged), encoding="utf-8")
            if generated_path != final_path:
                generated_path.replace(final_path)
        else:
            final_path.write_text(emit_transcript(merged, args.format) + "\n", encoding="utf-8")

    validate_output(final_path)
    print(f"Transcript written to: {final_path}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.from_json and args.input:
        parser.error("--from-json and input are mutually exclusive")
    if not args.from_json and not args.input:
        parser.error("input is required unless --from-json is used")

    if args.chunk_seconds < 0:
        parser.error("--chunk-seconds must be >= 0 (0 disables chunking)")
    if args.chunk_seconds and args.chunk_seconds < 10:
        parser.error("--chunk-seconds must be at least 10 seconds")
    if args.chunk_overlap < 0:
        parser.error("--chunk-overlap must be >= 0")
    if args.chunk_seconds and args.chunk_overlap >= args.chunk_seconds / 2:
        parser.error("--chunk-overlap must be smaller than half of --chunk-seconds")
    if not -100 <= args.silence_db <= 0:
        parser.error("--silence-db must be between -100 and 0")
    if args.silence_min <= 0:
        parser.error("--silence-min must be > 0")

    if args.from_json:
        if args.format == "json":
            allowed = ", ".join(f for f in SUPPORTED_FORMATS if f != "json")
            parser.error(f"--format must be one of {allowed} when using --from-json")
        if not args.from_json.exists() or not args.from_json.is_file():
            print(f"ERROR: JSON file not found: {args.from_json}", file=sys.stderr)
            sys.exit(1)

        ext = _format_file_ext(args.format)
        out_path = args.output if args.output else args.from_json.with_suffix(f".{ext}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        segments = load_vibevoice_segments(args.from_json)
        _warn_if_speakers_ignored(segments, args.format)
        out_path.write_text(emit_transcript(segments, args.format) + "\n", encoding="utf-8")
        print(f"Transcript written to: {out_path}")
        return

    ensure_apple_silicon()

    if not args.input.exists() or not args.input.is_file():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    try:
        # mlx-audio 0.4.5 has a circular import: stt.generate imports
        # stt.models, whose glmasr/voxtral modules import back from
        # stt.generate. Loading stt.models first breaks the cycle. Swallow
        # failures here so other mlx-audio versions without this submodule
        # (or with the cycle already fixed) still reach the real import below.
        with contextlib.suppress(ImportError):
            import mlx_audio.stt.models  # noqa: F401
        from mlx_audio.stt.generate import generate_transcription
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print("Run with: uv run ./audio_transcribe_vibevoice.py ...", file=sys.stderr)
        sys.exit(1)

    progress = None if args.no_progress else ProgressReporter(interval=args.progress_interval)
    if args.chunk_seconds:
        run_chunked(args, generate_transcription, progress)
        return

    final_path, mlx_stem, generated_path = resolve_output_paths(
        args.input,
        args.output,
        args.format,
    )
    old_mtime_ns = generated_path.stat().st_mtime_ns if generated_path.exists() else None

    progress = None if args.no_progress else ProgressReporter(interval=args.progress_interval)
    pre_convert = args.pre_convert_pcm16k or _bool_env("VIBEVOICE_PRECONVERT_PCM16K")
    audio_seconds = probe_media_duration(args.input, "ffprobe", args.verbose) if progress else None
    expected_seconds = audio_seconds * REALTIME_FACTOR if audio_seconds else None
    if progress:
        progress.info(f"Transcribing with {args.model}")
        progress.info(f"Writing {args.format.upper()} to {final_path}")
        if audio_seconds:
            progress.info(
                f"Audio duration {format_duration(audio_seconds)}; expect roughly "
                f"{format_duration(expected_seconds)} (~{REALTIME_FACTOR:g}x realtime)"
            )
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
                    expected_seconds=expected_seconds,
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
            _warn_if_speakers_ignored(segments, args.format)
            final_path.write_text(emit_transcript(segments, args.format) + "\n", encoding="utf-8")
        finally:
            generated_path.unlink(missing_ok=True)

    validate_output(final_path)
    print(f"Transcript written to: {final_path}")


if __name__ == "__main__":
    main()
