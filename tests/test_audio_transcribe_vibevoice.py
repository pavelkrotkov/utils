"""Tests for audio_transcribe_vibevoice chunked transcription (#95).

Covers silence-aware chunk planning, hard-cut overlap fallback, offset
splicing with seam dedupe, and silencedetect stderr parsing. No mlx-audio or
ffmpeg required: the exercised pieces are pure functions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_transcribe_vibevoice as vv
from audio_transcript import TranscriptSegment, emit_transcript

# ---------------------------------------------------------------------------
# plan_chunks
# ---------------------------------------------------------------------------


def test_plan_chunks_snaps_to_silence() -> None:
    # Silences centered at 290 and 590 sit inside the search bands around the
    # successive targets, so every cut snaps to silence: chunks butt-join with
    # no overlap anywhere.
    silences = [(289.0, 291.0), (589.0, 591.0)]
    chunks = vv.plan_chunks(900.0, 300.0, silences, overlap=2.5)
    assert [c.end for c in chunks] == pytest.approx([290.0, 590.0, 900.0])
    assert all(not c.overlaps_previous for c in chunks)
    assert chunks[0].start == 0.0
    # Chunks stay within the target size (+25% fold allowance).
    assert all(c.end - c.start <= 300.0 * 1.25 + 2.5 for c in chunks)


def test_plan_chunks_hard_cuts_with_overlap_without_silence() -> None:
    chunks = vv.plan_chunks(700.0, 300.0, [], overlap=2.5)
    # First cut is a hard cut at 300; the next chunk rewinds by the overlap.
    assert chunks[0].end == pytest.approx(300.0)
    assert chunks[1].start == pytest.approx(297.5)
    assert chunks[1].overlaps_previous is True
    # Remaining 402.5s still exceeds the fold threshold, so it splits once more.
    assert len(chunks) == 3
    assert [c.overlaps_previous for c in chunks] == [False, True, True]
    assert chunks[-1].end == pytest.approx(700.0)
    assert max(c.end - c.start for c in chunks) <= 300.0 * 1.25 + 2.5


def test_plan_chunks_folds_short_tail() -> None:
    chunks = vv.plan_chunks(340.0, 300.0, [(299.0, 299.6)], overlap=2.5)
    assert len(chunks) == 1
    assert chunks[0].end == pytest.approx(340.0)


def test_plan_chunks_single_pass_shape_for_tiny_file() -> None:
    chunks = vv.plan_chunks(120.0, 300.0, [], overlap=2.5)
    assert len(chunks) == 1
    assert not chunks[0].overlaps_previous


# ---------------------------------------------------------------------------
# merge_chunk_segments (offset splice + seam dedupe)
# ---------------------------------------------------------------------------


def _seg(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text)


def test_merge_shifts_local_times_to_absolute() -> None:
    chunks = [vv.Chunk(start=0.0, end=100.0), vv.Chunk(start=100.0, end=200.0)]
    merged = vv.merge_chunk_segments([[_seg(0.0, 4.0, "a")], [_seg(1.0, 5.0, "b")]], chunks)
    assert [(s.start, s.end) for s in merged] == [(0.0, 4.0), (101.0, 105.0)]


def test_merge_dedupes_overlapped_seam_by_coverage() -> None:
    # Hard cut at 300 with 2.5s overlap: chunk 2 starts at 297.5 and its first
    # segments re-transcribe the tail of chunk 1 ("dup words" ends at 300.2,
    # already covered). Only content past the covered frontier survives.
    chunks = [
        vv.Chunk(start=0.0, end=300.0),
        vv.Chunk(start=297.5, end=600.0, overlaps_previous=True),
    ]
    prev = [_seg(290.0, 300.2, "tail words")]
    nxt = [
        _seg(0.0, 2.7, "dup words"),  # abs 297.5-300.2 -> fully covered, dropped
        _seg(3.0, 12.0, "fresh"),  # abs 300.5-312.0 -> kept
    ]
    merged = vv.merge_chunk_segments([prev, nxt], chunks)
    assert [s.text for s in merged] == ["tail words", "fresh"]
    assert merged[1].start == pytest.approx(300.5)


def test_merge_keeps_all_segments_without_overlap_flag() -> None:
    # Butt join on silence: nothing may be dropped even if times touch.
    chunks = [vv.Chunk(start=0.0, end=290.0), vv.Chunk(start=290.0, end=580.0)]
    merged = vv.merge_chunk_segments(
        [[_seg(280.0, 289.9, "a")], [_seg(0.1, 5.0, "b")]],
        chunks,
    )
    assert [s.text for s in merged] == ["a", "b"]


def test_merged_output_is_monotonic_and_emits_all_formats() -> None:
    chunks = [
        vv.Chunk(start=0.0, end=300.0),
        vv.Chunk(start=297.5, end=600.0, overlaps_previous=True),
    ]
    merged = vv.merge_chunk_segments(
        [[_seg(0.0, 10.0, "hello"), _seg(290.0, 300.2, "tail")], [_seg(3.5, 20.0, "rest")]],
        chunks,
    )
    starts = [s.start for s in merged]
    assert starts == sorted(starts)

    txt = emit_transcript(merged, "txt")
    for word in ("hello", "tail", "rest"):
        assert txt.count(word) == 1  # no duplicated seam words in any format

    srt = emit_transcript(merged, "srt")
    assert "00:05:0" in srt  # absolute timestamps survived splicing (500s+)

    vtt = emit_transcript(merged, "vtt")
    assert "00:05:00" in vtt


def test_dump_segments_json_round_trips_through_loader(tmp_path: Path) -> None:
    segments = [
        TranscriptSegment(start=0.0, end=2.5, text="hi", speaker="Speaker 1"),
        TranscriptSegment(start=3.0, end=4.0, text="yo"),
    ]
    path = tmp_path / "combined.json"
    path.write_text(vv._dump_segments_json(segments), encoding="utf-8")

    loaded = vv.load_vibevoice_segments(path)
    assert [(s.start, s.end, s.text, s.speaker) for s in loaded] == [
        (0.0, 2.5, "hi", "Speaker 1"),
        (3.0, 4.0, "yo", None),
    ]


# ---------------------------------------------------------------------------
# detect_silences (stderr parsing)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess(SimpleNamespace):
    pass


def test_detect_silences_parses_ffmpeg_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tone = tmp_path / "audio.wav"
    tone.write_bytes(b"fake")

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return _FakeCompletedProcess(
            stdout="",
            stderr=(
                "[silencedetect @ 0x1] silence_start: 12.5\n"
                "[silencedetect @ 0x1] silence_end: 14.2 | silence_duration: 1.7\n"
                "[silencedetect @ 0x1] silence_start: 40\n"  # unterminated -> EOF
            ),
            returncode=0,
        )

    monkeypatch.setattr(vv.subprocess, "run", fake_run)
    monkeypatch.setattr(vv, "probe_media_duration", lambda *_args, **_kw: 55.0)

    silences = vv.detect_silences(tone, noise_db=-30.0, min_silence=0.5)
    assert silences == [(12.5, 14.2), (40.0, 55.0)]
    assert any("silencedetect=noise=-30" in part for part in captured_cmd[0])
    assert any(part == "d=0.5" or "d=0.5" in part for part in captured_cmd[0])


def test_detect_silences_survives_missing_ffmpeg(tmp_path: Path) -> None:
    missing = tmp_path / "audio.wav"
    missing.write_bytes(b"x")
    # OSError from subprocess surfaces as [] so chunk planning can fall back
    # to hard cuts instead of crashing.
    silences = vv.detect_silences(
        missing,
        noise_db=-30.0,
        min_silence=0.5,
        ffmpeg_bin="/nonexistent/ffmpeg",
    )
    assert silences == []


# ---------------------------------------------------------------------------
# extract_chunk command shape
# ---------------------------------------------------------------------------


def test_extract_chunk_builds_expected_ffmpeg_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "in.m4a"
    dest = tmp_path / "chunk.wav"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(vv.subprocess, "run", fake_run)
    vv.extract_chunk(src, dest, vv.Chunk(start=1.5, end=31.25))

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "1.500"
    assert "-to" in cmd and cmd[cmd.index("-to") + 1] == "31.250"
    assert "-ac" in cmd and "-ar" in cmd  # mono 16k conversion happens here
