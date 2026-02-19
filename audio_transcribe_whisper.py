#!/usr/bin/env python3
# /// script
# dependencies = [
#   "torch",
#   "pyannote.audio",
# ]
# ///
"""
audio_transcribe_whisper.py - Robust speech pipeline combining whisper-cpp ASR and pyannote diarization

Converts input media to mono 16 kHz WAV (via ffmpeg), runs whisper-cpp for ASR (JSON output),
runs pyannote.audio for speaker diarization, then merges results into a plain-text transcript
with speaker labels or break markers (no timestamps).

Designed for resilience: handles diverse JSON formats from different whisper-cpp builds,
gracefully falls back when timestamps/diarization are unavailable, and auto-configures
Metal acceleration on macOS.

Dependencies:
  - ffmpeg, whisper-cpp (CLI binaries)
  - Python: torch, pyannote.audio, argparse, json, pathlib, subprocess, tempfile, os, sys

Usage Examples:
  ./audio_transcribe_whisper.py input.m4a
  ./audio_transcribe_whisper.py input.m4a --speakers "Alice,Bob" --num-speakers 2
  ./audio_transcribe_whisper.py input.m4a --style breaks
  ./audio_transcribe_whisper.py input.m4a --no-ffmpeg --pyannote-model pyannote/speaker-diarization-community-1
"""

import argparse
import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

    diarization = pipeline(str(audio_path), **kwargs)
    return diarization


# ───────────────────────────────────────────────────────────────────────────────
# Merge ASR + Diarization
# ───────────────────────────────────────────────────────────────────────────────


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Compute positive overlap duration between [a0,a1] and [b0,b1]."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def merge_asr_with_diar(
    segments: List[Dict[str, Any]],
    diarization: Annotation,
    style: str,
    speaker_names: Optional[List[str]],
    verbose: bool = False,
) -> List[str]:
    """
    Merge ASR segments with diarization by assigning each ASR segment to best-matching speaker,
    then grouping consecutive segments with same speaker.
    Returns list of transcript lines (plain text with speaker labels or break markers).
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

        seg_speakers.append((best_speaker, s_text))

    # Normalize speaker labels: re-map to sequential SPEAKER_00, SPEAKER_01, etc.
    # based on order of first appearance
    unique_speakers = []
    speaker_map = {}
    for speaker, _ in seg_speakers:
        if speaker and speaker not in speaker_map:
            speaker_map[speaker] = f"SPEAKER_{len(unique_speakers):02d}"
            unique_speakers.append(speaker)

    # Apply mapping
    seg_speakers = [(speaker_map.get(spk, spk), txt) for spk, txt in seg_speakers]

    if verbose:
        print(
            f"INFO: Normalized {len(unique_speakers)} unique speakers: {list(speaker_map.values())}",
            file=sys.stderr,
        )

    # Group consecutive segments with same speaker
    lines = []
    if not seg_speakers:
        return lines

    current_speaker = seg_speakers[0][0]
    current_texts = [seg_speakers[0][1]] if seg_speakers[0][1] else []

    for speaker, text in seg_speakers[1:]:
        if speaker == current_speaker:
            if text:
                current_texts.append(text)
        else:
            # Emit current group
            if current_texts and current_speaker:
                combined = " ".join(current_texts).strip()
                if combined:
                    # Map speaker label to custom name if provided
                    display_label = current_speaker
                    if speaker_names and display_label.startswith("SPEAKER_"):
                        try:
                            idx = int(display_label.split("_")[1])
                            if idx < len(speaker_names):
                                display_label = speaker_names[idx]
                        except (IndexError, ValueError):
                            pass

                    if style == "breaks":
                        lines.append("--- speaker change ---")
                        lines.append(combined)
                    else:  # labels
                        lines.append(f"{display_label}: {combined}")

            # Start new group
            current_speaker = speaker
            current_texts = [text] if text else []

    # Emit final group
    if current_texts and current_speaker:
        combined = " ".join(current_texts).strip()
        if combined:
            display_label = current_speaker
            if speaker_names and display_label.startswith("SPEAKER_"):
                try:
                    idx = int(display_label.split("_")[1])
                    if idx < len(speaker_names):
                        display_label = speaker_names[idx]
                except (IndexError, ValueError):
                    pass

            if style == "breaks":
                lines.append("--- speaker change ---")
                lines.append(combined)
            else:  # labels
                lines.append(f"{display_label}: {combined}")

    return lines


# ───────────────────────────────────────────────────────────────────────────────
# Fallback: plain ASR transcript (no diarization)
# ───────────────────────────────────────────────────────────────────────────────


def plain_transcript(segments: List[Dict[str, Any]], fallback_text: Optional[str]) -> str:
    """Produce plain transcript from ASR segments or fallback text."""
    if fallback_text:
        return fallback_text.strip()
    texts = [_seg_text(s) for s in segments]
    return " ".join(t for t in texts if t).strip()


# ───────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ───────────────────────────────────────────────────────────────────────────────


def run_ffmpeg_convert(input_path: Path, output_path: Path, ffmpeg_bin: str, verbose: bool) -> None:
    """Convert input media to mono 16 kHz WAV using ffmpeg."""
    cmd = [
        ffmpeg_bin,
        "-y",
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
    if verbose:
        print(f"INFO: Running ffmpeg: {' '.join(cmd)}", file=sys.stderr)
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
    if language != "auto":
        cmd.extend(["-l", language])

    if verbose:
        print(f"INFO: Running whisper-cpp: {' '.join(cmd)}", file=sys.stderr)

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robust whisper-cpp + pyannote diarization pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", type=Path, help="Input media file")
    parser.add_argument(
        "-o", "--output", type=Path, help="Output transcript path (default: <input>.spk.txt)"
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="Path to ffmpeg binary")
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
    parser.add_argument("--hf-token", help="HuggingFace token (or set HF_TOKEN env)")
    parser.add_argument(
        "--pyannote-model",
        default="pyannote/speaker-diarization-3.1",
        help="Pyannote diarization model",
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
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Setup
    maybe_set_metal_env()
    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    speaker_names = [s.strip() for s in args.speakers.split(",")] if args.speakers else None
    args.whisper_bin = resolve_whisper_bin(args.whisper_bin, args.verbose)

    if not args.input.exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not args.large_model.exists():
        print(f"ERROR: Whisper model not found: {args.large_model}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or args.input.with_suffix(".spk.txt")

    # Temporary files
    temp_dir = tempfile.mkdtemp(prefix="whisper_pyannote_")
    temp_dir_path = Path(temp_dir)
    wav_path = temp_dir_path / "audio.wav"
    json_path = temp_dir_path / "whisper.json"

    try:
        # Step 1: Convert audio (or use original)
        if args.no_ffmpeg:
            audio_for_processing = args.input
            if args.verbose:
                print(
                    f"INFO: Skipping ffmpeg conversion; using original file: {args.input}",
                    file=sys.stderr,
                )
        else:
            run_ffmpeg_convert(args.input, wav_path, args.ffmpeg_bin, args.verbose)
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
        )

        # Step 3: Load ASR results
        segments, fallback_text = load_whisper_segments(json_path, args.verbose)

        # Check if segments have timestamps
        has_timestamps = any(_has_time(s) for s in segments)
        if not has_timestamps and not fallback_text:
            print("ERROR: ASR produced no usable segments.", file=sys.stderr)
            sys.exit(1)

        # Step 4: Run diarization
        pipeline = load_pyannote(args.pyannote_model, hf_token, args.verbose)
        diarization = run_diarization(
            pipeline,
            audio_for_processing,
            args.num_speakers,
            args.min_speakers,
            args.max_speakers,
            args.verbose,
        )

        # Step 5: Merge
        if has_timestamps:
            merged_lines = merge_asr_with_diar(
                segments, diarization, args.style, speaker_names, args.verbose
            )
        else:
            if args.verbose:
                print(
                    "WARNING: No timestamps in ASR segments; skipping diarization merge.",
                    file=sys.stderr,
                )
            merged_lines = []

        # Step 6: Write output
        if merged_lines:
            final_text = "\n".join(merged_lines)
        else:
            if args.verbose:
                print(
                    "WARNING: No merged output; falling back to plain ASR transcript.",
                    file=sys.stderr,
                )
            final_text = plain_transcript(segments, fallback_text)

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
