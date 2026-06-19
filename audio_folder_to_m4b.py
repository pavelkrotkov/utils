#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""Convert folders of audio files into M4B audiobooks with chapter markers.

Each book is one folder of audio tracks (``.mp3``, ``.m4a``, ``.mp4``, ...).
Tracks are ordered with a natural sort (so ``01, 02, ... 10`` sort correctly),
concatenated, re-encoded to AAC, and tagged with one chapter per track plus the
folder's cover image (if any) as embedded art.

Two modes:
  - Collection (default): every immediate subfolder of INPUT is its own book.
  - Single (``--single``): INPUT itself is one book.

Usage:
    # A whole collection (one .m4b per subfolder), output to a sibling dir:
    uv run ./audio_folder_to_m4b.py "/path/to/Audiobook Collection"

    # Only the subfolders whose name contains "Pandas":
    uv run ./audio_folder_to_m4b.py "/path/to/Collection" --book Pandas

    # A single book folder:
    uv run ./audio_folder_to_m4b.py "/path/to/Some Book" --single -o /path/to/out

    # Preview without encoding:
    uv run ./audio_folder_to_m4b.py "/path/to/Collection" --dry-run

Requires ffmpeg/ffprobe on PATH (``brew install ffmpeg``).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from audio_common import probe_media_duration

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".m4b", ".aac", ".wav", ".flac"}
COVER_EXTS = {".jpg", ".jpeg", ".png"}


def log(level: str, message: str) -> None:
    """Print a prefixed status message to stderr."""
    print(f"{level}: {message}", file=sys.stderr)


def natural_key(path: Path) -> list:
    """Sort key that handles embedded numbers correctly (01, 02, ... 10)."""
    # Use .name, not .stem: stem treats text after a dot as a suffix, so a
    # directory like "1.5 Special" would sort on "1" and could collide.
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def clean_title(stem: str) -> str:
    """Extract a human-readable chapter title from an audio filename stem."""
    # Strip leading disc-track pattern: "1-02 ", "4-12 - ", "1-02. "
    stem = re.sub(r"^\d+-\d+[-.\s]+", "", stem)
    # Strip leading track number plus any separator: "01 ", "01 - ", "1. "
    stem = re.sub(r"^\d+[-.\s]+", "", stem)
    # If there's a " - " separator, take the last segment as the chapter title
    if " - " in stem:
        stem = stem.rsplit(" - ", 1)[-1]
    return stem.strip()


def escape_meta(value: str) -> str:
    """Escape special characters for FFMETADATA values."""
    # Order matters: backslash first.
    for ch, esc in [("\\", "\\\\"), ("=", "\\="), (";", "\\;"), ("#", "\\#"), ("\n", "\\n")]:
        value = value.replace(ch, esc)
    return value


def find_audio_files(book_dir: Path) -> list[Path]:
    """Return the book's audio tracks in natural order."""
    try:
        entries = list(book_dir.iterdir())
    except OSError as e:
        log("WARNING", f"cannot read directory {book_dir.name}: {e}")
        return []
    return sorted(
        (f for f in entries if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
        key=natural_key,
    )


def find_cover(book_dir: Path) -> Path | None:
    """Return the first cover image in the folder, if any."""
    try:
        entries = sorted(book_dir.iterdir())
    except OSError as e:
        log("WARNING", f"cannot read directory {book_dir.name} for cover: {e}")
        return None
    images = [f for f in entries if f.is_file() and f.suffix.lower() in COVER_EXTS]
    if not images:
        return None
    preferred = ("cover", "front", "folder")
    # Exact conventional stem first (cover.jpg beats back_cover.jpg)...
    for name in preferred:
        for img in images:
            if img.stem.lower() == name:
                return img
    # ...then a substring match, then the alphabetically-first image.
    for name in preferred:
        for img in images:
            if name in img.stem.lower():
                return img
    return min(images, key=lambda p: p.name.lower())


def write_concat_list(tmp_dir: Path, audio_files: list[Path]) -> Path:
    concat_file = tmp_dir / "concat.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for p in audio_files:
            # resolve() to an absolute path: the manifest lives in a tempdir and
            # ffmpeg resolves relative concat entries against the manifest's dir,
            # not the cwd, so a relative INPUT would not be found otherwise.
            # as_posix() so the demuxer gets forward slashes (on Windows str(p)
            # would emit backslashes, which ffmpeg treats as escapes).
            escaped = p.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    return concat_file


def write_ffmetadata(
    tmp_dir: Path,
    book_title: str,
    artist: str | None,
    chapters: list[str],
    durations: list[float],
) -> Path:
    meta_file = tmp_dir / "metadata.txt"
    with open(meta_file, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={escape_meta(book_title)}\n")
        if artist:
            f.write(f"artist={escape_meta(artist)}\n")
        f.write(f"album={escape_meta(book_title)}\n")
        f.write("genre=Audiobook\n\n")

        # Accumulate as float and round each boundary so truncation can't drift
        # over many tracks; chapters stay contiguous (each START == prior END).
        cumulative_ms = 0.0
        for title, duration in zip(chapters, durations, strict=True):
            start_ms = round(cumulative_ms)
            cumulative_ms += duration * 1000
            end_ms = round(cumulative_ms)
            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={start_ms}\n")
            f.write(f"END={end_ms}\n")
            f.write(f"title={escape_meta(title)}\n\n")
    return meta_file


def probe_audio_stream(path: Path, ffprobe_bin: str) -> str | None:
    """Return 'codec,sample_rate,channels' for the first audio stream, or None."""
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0] if lines else None


def process_book(
    book_dir: Path,
    output_dir: Path,
    *,
    artist: str | None,
    bitrate: str,
    overwrite: bool,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    timeout: float | None,
) -> bool:
    book_name = book_dir.name
    output_file = output_dir / f"{book_name}.m4b"

    if output_file.exists() and not overwrite:
        log("INFO", f"skip (already exists): {output_file.name}")
        return True

    # Drop the output .m4b itself if it lives in the input folder (.m4b is in
    # AUDIO_EXTS), so an --overwrite rerun can't fold the old book back in.
    audio_files = [f for f in find_audio_files(book_dir) if f.resolve() != output_file.resolve()]
    if not audio_files:
        log("WARNING", f"no audio files found in {book_dir.name}")
        return False

    cover = find_cover(book_dir)

    log("INFO", f"probing {len(audio_files)} track(s) in {book_dir.name}...")
    usable_files: list[Path] = []
    durations: list[float] = []
    stream_params: set[str] = set()
    for af in audio_files:
        duration = probe_media_duration(af, ffprobe_bin, verbose=True)
        if duration is None:
            log("WARNING", f"skipping unreadable/zero-length track: {af.name}")
            continue
        usable_files.append(af)
        durations.append(duration)
        params = probe_audio_stream(af, ffprobe_bin)
        if params is not None:
            stream_params.add(params)

    if not usable_files:
        log("ERROR", f"no usable audio tracks in {book_dir.name}")
        return False

    # The concat demuxer needs uniform streams; mixed codec/rate/channels make
    # ffmpeg emit a garbled file while still exiting 0, so refuse up front.
    if len(stream_params) > 1:
        log(
            "ERROR",
            f"{book_name}: tracks have mismatched audio streams "
            f"({', '.join(sorted(stream_params))}); convert them to a common "
            "codec/sample-rate/channels first",
        )
        return False

    chapter_titles = [clean_title(f.stem) for f in usable_files]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        concat_file = write_concat_list(tmp_dir, usable_files)
        meta_file = write_ffmetadata(tmp_dir, book_name, artist, chapter_titles, durations)

        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(meta_file),
        ]
        map_args = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]

        if cover:
            cmd += ["-i", str(cover)]
            map_args += ["-map", "2", "-disposition:v", "attached_pic"]

        cmd += map_args
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-c:v", "copy", "-movflags", "+faststart"]
        # Encode to a temp file in the output dir and swap into place only on
        # success, so a failure (or --timeout) never destroys an existing book.
        # Keep the .m4b suffix so ffmpeg still infers the muxer from it.
        tmp_output = output_dir / f".{output_file.stem}.tmp{output_file.suffix}"
        cmd.append(str(tmp_output))

        total_mins = int(sum(durations) / 60)
        log("INFO", f"encoding {total_mins} min -> {output_file.name}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log("ERROR", f"ffmpeg timed out after {timeout}s for {book_name}")
            tmp_output.unlink(missing_ok=True)
            return False
        if result.returncode != 0:
            log("ERROR", f"ffmpeg failed for {book_name}:\n{result.stderr[-3000:]}")
            tmp_output.unlink(missing_ok=True)
            return False
        os.replace(tmp_output, output_file)

    print(output_file)
    return True


def collect_books(input_dir: Path, single: bool, book_filter: str | None) -> list[Path]:
    """Return the list of book folders to process."""
    if single:
        return [input_dir]

    try:
        entries = list(input_dir.iterdir())
    except OSError as e:
        log("ERROR", f"cannot read collection directory {input_dir}: {e}")
        return []
    book_dirs = sorted(
        (d for d in entries if d.is_dir() and find_audio_files(d)),
        key=natural_key,
    )
    if book_filter:
        book_dirs = [d for d in book_dirs if book_filter.lower() in d.name.lower()]
    return book_dirs


def default_output_dir(input_dir: Path, single: bool) -> Path:
    """Where to write .m4b files when --output-dir is not given."""
    if single:
        return input_dir.parent
    return input_dir.parent / f"{input_dir.name} M4B"


def require_binary(name: str) -> None:
    # which() only searches PATH; also accept an explicit path (e.g. --ffmpeg-bin
    # ./bin/ffmpeg), but require an executable file so a directory or non-exec
    # path fails here with a clear message instead of deep inside subprocess.
    p = Path(name)
    if shutil.which(name) or (p.is_file() and os.access(p, os.X_OK)):
        return
    log("ERROR", f"required binary not found or not executable: {name} (try: brew install ffmpeg)")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert folders of audio files into M4B audiobooks with chapters."
    )
    parser.add_argument(
        "input", type=Path, help="Collection directory (or a single book folder with --single)"
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Where to write .m4b files (default: sibling '<input> M4B')",
    )
    parser.add_argument(
        "--single", action="store_true", help="Treat INPUT as one book instead of a collection"
    )
    parser.add_argument(
        "--book", metavar="FILTER", help="Only process books whose folder name contains FILTER"
    )
    parser.add_argument("--artist", help="Artist/author metadata tag (omitted if not set)")
    parser.add_argument("--bitrate", default="64k", help="AAC audio bitrate (default: 64k)")
    parser.add_argument(
        "--overwrite", action="store_true", help="Re-encode books even if the .m4b already exists"
    )
    parser.add_argument("--dry-run", action="store_true", help="List books without converting")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-book ffmpeg timeout in seconds (default: no limit)",
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="Path to the ffmpeg binary")
    parser.add_argument("--ffprobe-bin", default="ffprobe", help="Path to the ffprobe binary")
    args = parser.parse_args()

    if not args.input.is_dir():
        log("ERROR", f"input directory not found: {args.input}")
        sys.exit(1)

    book_dirs = collect_books(args.input, args.single, args.book)
    if not book_dirs:
        log(
            "ERROR",
            "no book folders with audio found" + (f" matching '{args.book}'" if args.book else ""),
        )
        sys.exit(1)

    output_dir = args.output_dir or default_output_dir(args.input, args.single)

    if args.dry_run:
        log("INFO", f"{len(book_dirs)} book(s) | output: {output_dir}")
        for d in book_dirs:
            print(f"{d.name}  ({len(find_audio_files(d))} tracks)")
        return

    require_binary(args.ffmpeg_bin)
    require_binary(args.ffprobe_bin)
    output_dir.mkdir(parents=True, exist_ok=True)
    log("INFO", f"{len(book_dirs)} book(s) | output: {output_dir}")

    ok = err = 0
    for i, book_dir in enumerate(book_dirs, 1):
        log("INFO", f"[{i}/{len(book_dirs)}] {book_dir.name}")
        if process_book(
            book_dir,
            output_dir,
            artist=args.artist,
            bitrate=args.bitrate,
            overwrite=args.overwrite,
            ffmpeg_bin=args.ffmpeg_bin,
            ffprobe_bin=args.ffprobe_bin,
            timeout=args.timeout,
        ):
            ok += 1
        else:
            err += 1

    log("INFO", f"done: {ok} succeeded, {err} failed")
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
