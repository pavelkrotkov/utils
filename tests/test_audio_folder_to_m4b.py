"""Tests for audio_folder_to_m4b input/output separation and unified probing (#92).

Covers:
- .m4b is an output-only format: never scanned as an input track
- the two #91 corruption scenarios cannot recur without their guards
- probe_track probes duration + stream identity in ONE ffprobe call
- skip-unreadable / skip-no-audio / refuse-mismatched behaviors are preserved
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from audio_folder_to_m4b import (
    AUDIO_EXTS,
    collect_books,
    find_audio_files,
    probe_track,
    process_book,
)

_has_ffmpeg = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
skip_no_ffmpeg = pytest.mark.skipif(not _has_ffmpeg, reason="ffmpeg/ffprobe not on PATH")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tone(path: Path, *, seconds: float = 1.0, rate: int = 44100) -> Path:
    """Create a real audio file (sine wave) at the given path."""
    codecs = {".mp3": "libmp3lame", ".m4a": "aac", ".wav": "pcm_s16le"}
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
        "-c:a",
        codecs[path.suffix],
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _m4b_chapters(m4b: Path) -> list[str]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(m4b)],
        check=True,
        capture_output=True,
        text=True,
    )
    chapters = json.loads(result.stdout).get("chapters", [])
    return [chapter.get("tags", {}).get("title", "") for chapter in chapters]


def _process(book_dir: Path, output_dir: Path, *, overwrite: bool = False) -> bool:
    # Mirror main(), which creates the output dir before processing.
    output_dir.mkdir(parents=True, exist_ok=True)
    return process_book(
        book_dir,
        output_dir,
        artist=None,
        bitrate="64k",
        overwrite=overwrite,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Input/output format separation
# ---------------------------------------------------------------------------


def test_m4b_is_not_an_input_extension() -> None:
    assert ".m4b" not in AUDIO_EXTS


def test_output_dir_inside_collection_is_not_a_book(tmp_path: Path) -> None:
    """#91 corruption scenario A: an output dir inside the collection must not
    scan as a book, even though it contains this tool's own outputs."""
    collection = tmp_path / "Collection"
    book = collection / "BookA"
    book.mkdir(parents=True)
    (book / "01.mp3").touch()
    out_dir = collection / "Output M4B"
    out_dir.mkdir()
    (out_dir / "BookA.m4b").touch()  # prior output, zero bytes

    assert find_audio_files(out_dir) == []
    assert collect_books(collection, single=False, book_filter=None) == [book]


@skip_no_ffmpeg
def test_overwrite_does_not_fold_old_book_back_in(tmp_path: Path) -> None:
    """#91 corruption scenario B: --output-dir == book folder + --overwrite must
    not re-ingest the previous .m4b as a third track."""
    book_dir = tmp_path / "Book"
    book_dir.mkdir()
    _make_tone(book_dir / "01 - Alpha.m4a")
    _make_tone(book_dir / "02 - Beta.m4a")

    assert _process(book_dir, book_dir, overwrite=False) is True
    output_file = book_dir / "Book.m4b"
    assert output_file.exists()

    # Rerun with --overwrite: the old Book.m4b sits right in the input folder.
    assert _process(book_dir, book_dir, overwrite=True) is True
    assert _m4b_chapters(output_file) == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Unified probing
# ---------------------------------------------------------------------------


@skip_no_ffmpeg
def test_probe_track_single_ffprobe_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tone = _make_tone(tmp_path / "track.m4a")

    ffprobe_calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(cmd, **kwargs):
        if cmd and cmd[0] == "ffprobe":
            ffprobe_calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("audio_folder_to_m4b.subprocess.run", counting_run)

    probed = probe_track(tone, "ffprobe")
    assert probed is not None
    duration, identity = probed
    assert ffprobe_calls, "expected exactly one ffprobe invocation"
    assert len(ffprobe_calls) == 1
    assert abs(duration - 1.0) < 0.3
    # Identity carries codec/sample-rate/channels for the uniformity check.
    parts = identity.split(",")
    assert len(parts) == 3
    assert parts[0] == "aac"


@skip_no_ffmpeg
def test_probe_track_video_only_returns_none(tmp_path: Path) -> None:
    """A container duration exists but there is no audio stream to encode."""
    video_only = tmp_path / "stray.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=1:r=5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        check=True,
        capture_output=True,
    )
    assert probe_track(video_only, "ffprobe") is None


@skip_no_ffmpeg
def test_probe_track_unreadable_returns_none(tmp_path: Path) -> None:
    garbage = tmp_path / "broken.mp3"
    garbage.write_bytes(b"this is not audio")
    assert probe_track(garbage, "ffprobe") is None


@skip_no_ffmpeg
def test_mismatched_streams_are_refused(tmp_path: Path) -> None:
    """Mixed sample rates must still be refused fast through the new probe."""
    book_dir = tmp_path / "Mixed"
    book_dir.mkdir()
    _make_tone(book_dir / "01.m4a", rate=44100)
    _make_tone(book_dir / "02.m4a", rate=48000)
    out_dir = tmp_path / "Out"

    assert _process(book_dir, out_dir) is False
    assert not list(out_dir.glob("*.m4b"))


@skip_no_ffmpeg
def test_audioless_track_is_skipped(tmp_path: Path) -> None:
    """A video-only stray file is skipped; remaining tracks still convert."""
    book_dir = tmp_path / "Book"
    book_dir.mkdir()
    _make_tone(book_dir / "01 - Alpha.m4a")
    video_only = book_dir / "02 - Stray.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=1:r=5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        check=True,
        capture_output=True,
    )
    out_dir = tmp_path / "Out"

    assert _process(book_dir, out_dir) is True
    m4b = out_dir / "Book.m4b"
    assert m4b.exists()
    assert _m4b_chapters(m4b) == ["Alpha"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@skip_no_ffmpeg
def test_two_track_book_converts_with_chapters(tmp_path: Path) -> None:
    book_dir = tmp_path / "Book"
    book_dir.mkdir()
    _make_tone(book_dir / "01 - Alpha.mp3")
    _make_tone(book_dir / "02 - Beta.mp3")
    out_dir = tmp_path / "Out"

    assert _process(book_dir, out_dir) is True
    m4b = out_dir / "Book.m4b"
    assert m4b.exists()

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(m4b)],
        check=True,
        capture_output=True,
        text=True,
    )
    total = float(json.loads(result.stdout)["format"]["duration"])
    assert abs(total - 2.0) < 0.6
