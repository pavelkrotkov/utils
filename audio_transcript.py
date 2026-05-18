"""Shared transcript segment model and text emitters for audio utilities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence


@dataclass(frozen=True)
class TranscriptSegment:
    """A normalized transcript segment with second-based timing."""

    start: float
    end: float
    text: str
    speaker: str | None = None


SpeakerNames = Mapping[str, str] | Sequence[str] | None


def emit_txt(segments: Sequence[TranscriptSegment]) -> str:
    """Emit plain text, one non-empty segment per line."""
    return "\n".join(segment.text.strip() for segment in segments if segment.text.strip())


def emit_srt(segments: Sequence[TranscriptSegment]) -> str:
    """Emit SubRip subtitle text."""
    blocks = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        index = len(blocks) + 1
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{_format_srt_timestamp(segment.start)} --> "
                    f"{_format_srt_timestamp(segment.end)}",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks)


def emit_vtt(segments: Sequence[TranscriptSegment]) -> str:
    """Emit WebVTT subtitle text.

    Empty input still emits the required WEBVTT file header, unlike plain text
    and SRT where an empty transcript can be represented by an empty file.
    """
    cues = ["WEBVTT"]
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        cues.append(
            "\n".join(
                [
                    f"{_format_vtt_timestamp(segment.start)} --> "
                    f"{_format_vtt_timestamp(segment.end)}",
                    text,
                ]
            )
        )
    return "\n\n".join(cues)


def emit_diarized_txt(
    segments: Sequence[TranscriptSegment],
    speaker_names: SpeakerNames = None,
) -> str:
    """Emit speaker-labeled text, grouping adjacent segments for the same speaker."""
    lines: list[str] = []
    current_speaker: str | None = None
    current_text: list[str] = []

    def flush() -> None:
        if not current_text:
            return
        text = " ".join(part for part in current_text if part).strip()
        if not text:
            return
        if current_speaker:
            lines.append(f"{_display_speaker(current_speaker, speaker_names)}: {text}")
        else:
            lines.append(text)

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if segment.speaker != current_speaker:
            flush()
            current_speaker = segment.speaker
            current_text = [text]
        else:
            current_text.append(text)

    flush()
    return "\n".join(lines)


def emit_transcript(
    segments: Sequence[TranscriptSegment],
    output_format: str,
    speaker_names: SpeakerNames = None,
) -> str:
    """Dispatch to a transcript emitter by CLI format name."""
    if output_format == "txt":
        return emit_txt(segments)
    if output_format == "srt":
        return emit_srt(segments)
    if output_format == "vtt":
        return emit_vtt(segments)
    if output_format == "diarized-txt":
        return emit_diarized_txt(segments, speaker_names)
    raise ValueError(f"Unsupported transcript format: {output_format}")


def remap_speakers(
    segments: Sequence[TranscriptSegment],
    speaker_names: SpeakerNames,
) -> list[TranscriptSegment]:
    """Return segments with speaker labels remapped by first speaker appearance."""
    if speaker_names is None:
        return list(segments)

    speaker_map: dict[str, str] = {}
    remapped: list[TranscriptSegment] = []
    for segment in segments:
        speaker = segment.speaker
        if speaker is None:
            remapped.append(segment)
            continue
        if speaker not in speaker_map:
            speaker_map[speaker] = _display_speaker(speaker, speaker_names, len(speaker_map))
        remapped.append(replace(segment, speaker=speaker_map[speaker]))
    return remapped


def _display_speaker(
    speaker: str,
    speaker_names: SpeakerNames,
    appearance_index: int | None = None,
) -> str:
    if speaker_names is None:
        return speaker

    if isinstance(speaker_names, Mapping):
        return speaker_names.get(speaker, speaker)

    index = appearance_index
    if index is None and speaker.startswith("SPEAKER_"):
        try:
            index = int(speaker.rsplit("_", 1)[1])
        except ValueError:
            index = None

    if index is not None and 0 <= index < len(speaker_names):
        name = speaker_names[index].strip()
        if name:
            return name

    return speaker


def _format_srt_timestamp(seconds: float) -> str:
    hours, minutes, whole_seconds, milliseconds = _timestamp_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    hours, minutes, whole_seconds, milliseconds = _timestamp_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _timestamp_parts(seconds: float) -> tuple[int, int, int, int]:
    total_milliseconds = max(0, int(round(float(seconds) * 1000)))
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    minutes_total, whole_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return hours, minutes, whole_seconds, milliseconds
