"""One place to turn an ASR backend's JSON into transcript segments.

whisper-cpp and mlx-audio (VibeVoice) both emit loosely-shaped JSON, and both
scripts used to decode it independently — the same container shapes, the same
time units, the same key aliases, written twice. This module owns that
decoding; the backends differ only in which file they hand over.

What it absorbs:

    container   a list, a dict keyed by numeric strings, a nested list, a bare
                string, or a JSON array serialised into a "text" field (older
                mlx-audio, sometimes truncated mid-object)
    location    "segments", "chunks", "transcription", "results", or nested
    time units  seconds, ``t0``/``t1`` centiseconds, ``offsets.from``/``to``
                milliseconds
    text keys   "text", "content", "utterance", "transcript"
    speakers    "speaker", "speaker_id", "speaker_label"
    key case    older mlx-audio capitalised its keys

:class:`Transcript` reports how many segments carried an explicit start time.
whisper-cpp needs that distinction: with no timestamps anywhere there is
nothing to align diarization against, and the merge is skipped rather than
silently producing unlabelled output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_transcript import TranscriptSegment

SEGMENT_CONTAINER_KEYS = ("segments", "chunks", "transcription", "results")
TEXT_KEYS = ("text", "content", "utterance", "transcript")
SPEAKER_KEYS = ("speaker", "speaker_id", "speaker_label")
START_KEYS = ("start", "start_time", "begin", "ts", "t0")
END_KEYS = ("end", "end_time", "finish", "te", "t1")
#: Keys that mark a mapping as a segment rather than arbitrary JSON.
SEGMENT_KEYS = frozenset(TEXT_KEYS + SPEAKER_KEYS + START_KEYS + END_KEYS + ("offsets",))


class TranscriptError(Exception):
    """ASR output could not be decoded into transcript segments."""


@dataclass(frozen=True)
class Transcript:
    """Segments decoded from one ASR backend's output.

    ``fallback_text`` is a whole-transcript string some backends emit alongside
    (or instead of) segments. ``timed_count`` is how many segments carried an
    explicit start time; segments without one are placed at 0.0.
    """

    segments: list[TranscriptSegment] = field(default_factory=list)
    fallback_text: str | None = None
    timed_count: int = 0

    @property
    def has_timing(self) -> bool:
        """Whether any segment carried a real timestamp."""
        return self.timed_count > 0

    def __bool__(self) -> bool:
        return bool(self.segments or self.fallback_text)

    def __len__(self) -> int:
        return len(self.segments)

    def resolved_segments(self) -> list[TranscriptSegment]:
        """Return the segments, or a single segment holding the fallback text."""
        if self.segments:
            return list(self.segments)
        if self.fallback_text and self.fallback_text.strip():
            return [TranscriptSegment(start=0.0, end=0.0, text=self.fallback_text.strip())]
        return []

    def plain_text(self) -> str:
        """Return the transcript as one plain string."""
        if self.fallback_text and self.fallback_text.strip():
            return self.fallback_text.strip()
        return " ".join(segment.text for segment in self.segments if segment.text).strip()


def read_transcript_file(json_path: Path, *, label: str = "ASR") -> Transcript:
    """Read and decode a backend's JSON output.

    Raises:
        TranscriptError: the file is missing or is not valid JSON.
    """
    if not json_path.exists():
        raise TranscriptError(f"{label} JSON not found: {json_path}")

    try:
        with open(json_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"Invalid {label} JSON in {json_path}: {exc}") from exc
    except OSError as exc:
        raise TranscriptError(f"Unable to read {label} JSON {json_path}: {exc}") from exc

    return read_transcript(data)


def read_transcript(data: Any) -> Transcript:
    """Decode already-parsed ASR JSON into a :class:`Transcript`.

    Never raises on shape: unrecognised input yields an empty transcript, which
    callers report in their own words.
    """
    raw_segments = _find_segment_list(data)
    decoded = [_segment_from_raw(item) for item in raw_segments]

    segments = [segment for segment, _ in decoded if segment.text.strip()]
    timed_count = sum(1 for segment, timed in decoded if timed and segment.text.strip())

    if timed_count:
        segments.sort(key=lambda segment: (segment.start, segment.end))

    return Transcript(
        segments=segments,
        fallback_text=_fallback_text(data),
        timed_count=timed_count,
    )


# --------------------------------------------------------------------------
# Locating the segment list
# --------------------------------------------------------------------------


def _find_segment_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data if _looks_like_segment_list(data) else []
    if isinstance(data, str):
        return [data] if data.strip() else []
    if not isinstance(data, dict):
        return []

    for key in SEGMENT_CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            if _looks_like_segment_list(value):
                return value
            continue
        if isinstance(value, dict):
            # whisper-cpp sometimes keys segments by their index, as strings.
            ordered = _values_in_numeric_key_order(value)
            if ordered is not None and _looks_like_segment_list(ordered):
                return ordered
            nested = _find_segment_list(value)
            if nested:
                return nested

    # Older mlx-audio serialised the segment array as a JSON string, sometimes
    # truncated mid-object; recover whatever objects are complete.
    for key in ("text", "content"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        for candidate in _json_array_candidates(value):
            if isinstance(candidate, list) and _looks_like_segment_list(candidate):
                return candidate

    return []


def _values_in_numeric_key_order(value: dict[str, Any]) -> list[Any] | None:
    try:
        keys = sorted(value, key=int)
    except (TypeError, ValueError):
        return None
    return [value[key] for key in keys]


def _json_array_candidates(text: str) -> list[Any]:
    """Yield parseable JSON array candidates, including a truncation fallback."""
    if not text.lstrip().startswith("["):
        return []
    try:
        return [json.loads(text)]
    except (json.JSONDecodeError, ValueError):
        pass

    position = len(text)
    while (position := text.rfind("}", 0, position)) >= 0:
        try:
            return [json.loads(text[: position + 1] + "]")]
        except (json.JSONDecodeError, ValueError):
            continue
    return []


def _looks_like_segment_list(value: list[Any]) -> bool:
    return any(isinstance(item, (str, list)) or _looks_like_segment(item) for item in value)


def _looks_like_segment(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool({key.lower() for key in value} & SEGMENT_KEYS)


def _fallback_text(data: Any) -> str | None:
    if isinstance(data, str):
        return data or None
    if not isinstance(data, dict):
        return None
    for key in TEXT_KEYS:
        value = data.get(key)
        # A serialised segment array in "text" is not fallback prose.
        if isinstance(value, str) and value.strip() and not _json_array_candidates(value):
            return value
    return None


# --------------------------------------------------------------------------
# Decoding one segment
# --------------------------------------------------------------------------


def _segment_from_raw(raw: Any) -> tuple[TranscriptSegment, bool]:
    """Decode one raw item. Returns the segment and whether it carried a start time."""
    if isinstance(raw, list):
        # A nested list: keep whatever text it flattens to.
        joined = " ".join(_segment_from_raw(item)[0].text for item in raw).strip()
        return TranscriptSegment(start=0.0, end=0.0, text=joined), False
    if not isinstance(raw, dict):
        text = raw if isinstance(raw, str) else str(raw)
        return TranscriptSegment(start=0.0, end=0.0, text=text.strip()), False

    # Older mlx-audio capitalised its keys (Start, End, Speaker, Content).
    if any(key[:1].isupper() for key in raw):
        raw = {key.lower(): value for key, value in raw.items()}

    start = _extract_time(raw, START_KEYS)
    end = _extract_time(raw, END_KEYS)

    offsets = raw.get("offsets")
    if isinstance(offsets, dict):
        if start is None and "from" in offsets:
            start = _coerce_seconds(offsets["from"], milliseconds=True)
        if end is None and "to" in offsets:
            end = _coerce_seconds(offsets["to"], milliseconds=True)

    timed = start is not None
    resolved_start = 0.0 if start is None else start
    resolved_end = resolved_start if end is None or end < resolved_start else end

    speaker = None
    for key in SPEAKER_KEYS:
        value = raw.get(key)
        if value is not None:
            speaker = str(value)
            break

    return (
        TranscriptSegment(
            start=resolved_start,
            end=resolved_end,
            text=_extract_text(raw),
            speaker=speaker,
        ),
        timed,
    )


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in TEXT_KEYS:
        if key in value:
            return str(value[key]).strip()
    return ""


def _extract_time(value: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in value:
            # whisper-cpp reports t0/t1 in centiseconds.
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
