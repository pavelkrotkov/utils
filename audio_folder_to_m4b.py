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
    parts = re.split(r"(\d+)", path.stem.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def clean_title(stem: str) -> str:
    """Extract a human-readable chapter title from an audio filename stem."""
    # Strip leading disc-track pattern: "1-02 " or "4-12 "
    stem = re.sub(r"^\d+-\d+\s+", "", stem)
    # Strip leading track number: "01 " or "1 "
    stem = re.sub(r"^\d+\s+", "", stem)
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
    return sorted(
        (f for f in book_dir.iterdir() if f.suffix.lower() in AUDIO_EXTS),
        key=natural_key,
    )


def find_cover(book_dir: Path) -> Path | None:
    """Return the first cover image in the folder, if any."""
    return next(
        (f for f in sorted(book_dir.iterdir()) if f.suffix.lower() in COVER_EXTS),
        None,
    )


def write_concat_list(tmp_dir: Path, audio_files: list[Path]) -> Path:
    concat_file = tmp_dir / "concat.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for p in audio_files:
            escaped = str(p).replace("'", "'\\''")
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

        pos_ms = 0
        for title, duration in zip(chapters, durations, strict=True):
            end_ms = pos_ms + int(duration * 1000)
            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={pos_ms}\n")
            f.write(f"END={end_ms}\n")
            f.write(f"title={escape_meta(title)}\n\n")
            pos_ms = end_ms
    return meta_file


def process_book(
    book_dir: Path,
    output_dir: Path,
    *,
    artist: str | None,
    bitrate: str,
    overwrite: bool,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> bool:
    book_name = book_dir.name
    output_file = output_dir / f"{book_name}.m4b"

    if output_file.exists() and not overwrite:
        log("INFO", f"skip (already exists): {output_file.name}")
        return True

    audio_files = find_audio_files(book_dir)
    if not audio_files:
        log("WARNING", f"no audio files found in {book_dir.name}")
        return False

    cover = find_cover(book_dir)

    log("INFO", f"probing {len(audio_files)} track(s) in {book_dir.name}...")
    usable_files: list[Path] = []
    durations: list[float] = []
    for af in audio_files:
        duration = probe_media_duration(af, ffprobe_bin, verbose=True)
        if duration is None:
            log("WARNING", f"skipping unreadable/zero-length track: {af.name}")
            continue
        usable_files.append(af)
        durations.append(duration)

    if not usable_files:
        log("ERROR", f"no usable audio tracks in {book_dir.name}")
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
        cmd.append(str(output_file))

        total_mins = int(sum(durations) / 60)
        log("INFO", f"encoding {total_mins} min -> {output_file.name}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log("ERROR", f"ffmpeg failed for {book_name}:\n{result.stderr[-3000:]}")
            output_file.unlink(missing_ok=True)
            return False

    print(output_file)
    return True


def collect_books(input_dir: Path, single: bool, book_filter: str | None) -> list[Path]:
    """Return the list of book folders to process."""
    if single:
        return [input_dir]

    book_dirs = sorted(
        (d for d in input_dir.iterdir() if d.is_dir() and find_audio_files(d)),
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
    if not shutil.which(name):
        log("ERROR", f"required binary not found on PATH: {name} (try: brew install ffmpeg)")
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
        ):
            ok += 1
        else:
            err += 1

    log("INFO", f"done: {ok} succeeded, {err} failed")
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
