#!/usr/bin/env python3
"""Unit tests for the shared ASR transcript-ingestion module.

The table below is the point of the module: every row is a shape one of the two
backends actually emits, and both now go through the same decoder.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audio_segments import (
    Transcript,
    TranscriptError,
    read_transcript,
    read_transcript_file,
)
from audio_transcript import TranscriptSegment


class ContainerShapeTest(unittest.TestCase):
    """Where the segment list hides, per backend."""

    def test_plain_list_of_segments(self) -> None:
        data = [{"start": 0.0, "end": 1.0, "text": "hello"}]

        self.assertEqual(read_transcript(data).segments, [TranscriptSegment(0.0, 1.0, "hello")])

    def test_segments_key(self) -> None:
        data = {"segments": [{"start": 1.0, "end": 2.0, "text": "hi"}]}

        self.assertEqual(read_transcript(data).segments, [TranscriptSegment(1.0, 2.0, "hi")])

    def test_alternate_container_keys(self) -> None:
        for key in ("chunks", "transcription", "results"):
            with self.subTest(key=key):
                data = {key: [{"start": 0.0, "end": 1.0, "text": "hi"}]}

                self.assertEqual(len(read_transcript(data)), 1)

    def test_nested_container(self) -> None:
        data = {
            "results": {
                "metadata": {"ids": [1, 2, 3]},
                "segments": [{"start": 1.0, "end": 2.0, "text": "hello"}],
            }
        }

        self.assertEqual(read_transcript(data).segments, [TranscriptSegment(1.0, 2.0, "hello")])

    def test_unrelated_lists_under_segment_keys_are_ignored(self) -> None:
        data = {
            "results": {
                "segments": [{"id": 123, "score": 0.9}],
                "text": "fallback transcript",
            }
        }
        transcript = read_transcript(data)

        self.assertEqual(transcript.segments, [])
        # Fallback text is only read from the top level, as both backends did.
        self.assertIsNone(transcript.fallback_text)

    def test_dict_keyed_by_numeric_strings(self) -> None:
        data = {
            "segments": {
                "1": {"start": 1.0, "end": 2.0, "text": "second"},
                "0": {"start": 0.0, "end": 1.0, "text": "first"},
            }
        }

        self.assertEqual(
            [segment.text for segment in read_transcript(data).segments],
            ["first", "second"],
        )

    def test_bare_string_payload(self) -> None:
        self.assertEqual(
            read_transcript("just text").segments, [TranscriptSegment(0.0, 0.0, "just text")]
        )

    def test_nested_lists_are_flattened(self) -> None:
        data = {"segments": [["a", "b"]]}

        self.assertEqual(read_transcript(data).segments, [TranscriptSegment(0.0, 0.0, "a b")])

    def test_segment_array_serialised_into_a_text_field(self) -> None:
        """Older mlx-audio stringified the array into "text"."""
        payload = json.dumps([{"start": 0.0, "end": 1.0, "text": "hello"}])
        transcript = read_transcript({"text": payload})

        self.assertEqual(transcript.segments, [TranscriptSegment(0.0, 1.0, "hello")])
        self.assertIsNone(transcript.fallback_text)

    def test_truncated_serialised_array_recovers_complete_objects(self) -> None:
        payload = '[{"start": 0.0, "end": 1.0, "text": "kept"}, {"start": 1.0, "te'
        transcript = read_transcript({"text": payload})

        self.assertEqual(transcript.segments, [TranscriptSegment(0.0, 1.0, "kept")])

    def test_unrecognised_input_yields_an_empty_transcript(self) -> None:
        for data in (None, 17, {}, [], {"unrelated": [1, 2, 3]}):
            with self.subTest(data=data):
                transcript = read_transcript(data)

                self.assertEqual(transcript.segments, [])
                self.assertFalse(transcript)


class TimeUnitTest(unittest.TestCase):
    """Each backend reports time in its own unit."""

    def test_seconds_keys(self) -> None:
        for start_key, end_key in (
            ("start", "end"),
            ("start_time", "end_time"),
            ("begin", "finish"),
            ("ts", "te"),
        ):
            with self.subTest(keys=(start_key, end_key)):
                data = [{start_key: 1.5, end_key: 2.5, "text": "hi"}]

                self.assertEqual(read_transcript(data).segments[0].start, 1.5)
                self.assertEqual(read_transcript(data).segments[0].end, 2.5)

    def test_t0_and_t1_are_centiseconds(self) -> None:
        data = [{"t0": 150, "t1": 250, "text": "hi"}]
        segment = read_transcript(data).segments[0]

        self.assertAlmostEqual(segment.start, 1.5)
        self.assertAlmostEqual(segment.end, 2.5)

    def test_offsets_are_milliseconds(self) -> None:
        data = [{"offsets": {"from": 1500, "to": 2500}, "text": "hi"}]
        segment = read_transcript(data).segments[0]

        self.assertAlmostEqual(segment.start, 1.5)
        self.assertAlmostEqual(segment.end, 2.5)

    def test_explicit_keys_win_over_offsets(self) -> None:
        data = [{"start": 9.0, "end": 10.0, "offsets": {"from": 1500, "to": 2500}, "text": "hi"}]
        segment = read_transcript(data).segments[0]

        self.assertEqual((segment.start, segment.end), (9.0, 10.0))

    def test_unparseable_times_fall_back_to_zero(self) -> None:
        segment = read_transcript([{"start": "abc", "text": "hi"}]).segments[0]

        self.assertEqual((segment.start, segment.end), (0.0, 0.0))

    def test_end_before_start_is_clamped(self) -> None:
        segment = read_transcript([{"start": 5.0, "end": 1.0, "text": "hi"}]).segments[0]

        self.assertEqual((segment.start, segment.end), (5.0, 5.0))

    def test_missing_end_defaults_to_start(self) -> None:
        segment = read_transcript([{"start": 5.0, "text": "hi"}]).segments[0]

        self.assertEqual((segment.start, segment.end), (5.0, 5.0))


class TextAndSpeakerTest(unittest.TestCase):
    def test_text_key_aliases(self) -> None:
        for key in ("text", "content", "utterance", "transcript"):
            with self.subTest(key=key):
                data = [{"start": 0.0, key: "  hi  "}]

                self.assertEqual(read_transcript(data).segments[0].text, "hi")

    def test_speaker_key_aliases(self) -> None:
        for key in ("speaker", "speaker_id", "speaker_label"):
            with self.subTest(key=key):
                data = [{"start": 0.0, "text": "hi", key: 3}]

                self.assertEqual(read_transcript(data).segments[0].speaker, "3")

    def test_capitalised_keys_are_accepted(self) -> None:
        """Older mlx-audio emitted Start/End/Speaker/Content."""
        data = [{"Start": 1.0, "End": 2.0, "Content": "hello", "Speaker": "A"}]

        self.assertEqual(
            read_transcript(data).segments,
            [TranscriptSegment(1.0, 2.0, "hello", "A")],
        )

    def test_empty_text_segments_are_dropped(self) -> None:
        data = [
            {"start": 0.0, "end": 1.0, "text": "  "},
            {"start": 1.0, "end": 2.0, "text": "kept"},
        ]

        self.assertEqual(read_transcript(data).segments, [TranscriptSegment(1.0, 2.0, "kept")])


class TimingAwarenessTest(unittest.TestCase):
    """whisper skips the diarization merge when nothing carries a timestamp."""

    def test_timed_segments_are_counted(self) -> None:
        data = [{"start": 0.0, "text": "a"}, {"text": "b"}]
        transcript = read_transcript(data)

        self.assertTrue(transcript.has_timing)
        self.assertEqual(transcript.timed_count, 1)

    def test_untimed_output_reports_no_timing(self) -> None:
        transcript = read_transcript([{"text": "a"}, {"text": "b"}])

        self.assertFalse(transcript.has_timing)
        self.assertEqual(transcript.timed_count, 0)

    def test_timed_segments_are_sorted(self) -> None:
        data = [
            {"start": 5.0, "end": 6.0, "text": "later"},
            {"start": 1.0, "end": 2.0, "text": "earlier"},
        ]

        self.assertEqual(
            [segment.text for segment in read_transcript(data).segments],
            ["earlier", "later"],
        )

    def test_untimed_segments_keep_their_order(self) -> None:
        data = [{"text": "first"}, {"text": "second"}]

        self.assertEqual(
            [segment.text for segment in read_transcript(data).segments],
            ["first", "second"],
        )


class FallbackTextTest(unittest.TestCase):
    def test_top_level_text_is_the_fallback(self) -> None:
        transcript = read_transcript({"text": "whole transcript"})

        self.assertEqual(transcript.fallback_text, "whole transcript")
        self.assertEqual(
            transcript.resolved_segments(),
            [TranscriptSegment(0.0, 0.0, "whole transcript")],
        )

    def test_segments_take_precedence_over_fallback_text(self) -> None:
        data = {"text": "whole", "segments": [{"start": 0.0, "end": 1.0, "text": "part"}]}
        transcript = read_transcript(data)

        self.assertEqual(transcript.resolved_segments(), [TranscriptSegment(0.0, 1.0, "part")])
        self.assertEqual(transcript.plain_text(), "whole")

    def test_plain_text_joins_segments_without_a_fallback(self) -> None:
        data = [{"start": 0.0, "text": "one"}, {"start": 1.0, "text": "two"}]

        self.assertEqual(read_transcript(data).plain_text(), "one two")

    def test_empty_transcript_resolves_to_nothing(self) -> None:
        self.assertEqual(Transcript().resolved_segments(), [])
        self.assertEqual(Transcript().plain_text(), "")


class ReadFileTest(unittest.TestCase):
    def test_reads_json_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "asr.json"
            path.write_text(
                json.dumps({"segments": [{"start": 0.0, "end": 1.0, "text": "hi"}]}),
                encoding="utf-8",
            )

            self.assertEqual(len(read_transcript_file(path)), 1)

    def test_missing_file_uses_the_callers_label(self) -> None:
        with self.assertRaisesRegex(TranscriptError, "Whisper JSON not found"):
            read_transcript_file(Path("/definitely/missing.json"), label="Whisper")

    def test_invalid_json_uses_the_callers_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "asr.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(TranscriptError, "Invalid VibeVoice JSON"):
                read_transcript_file(path, label="VibeVoice")


class BackendWiringTest(unittest.TestCase):
    """Neither script may keep its own copy of the decoding rules."""

    PRIVATE_DECODERS = (
        "_normalize_segments",
        "_seg_start",
        "_seg_end",
        "_seg_text",
        "_has_time",
        "_extract_raw_segments",
        "_segment_from_raw",
        "_extract_time",
        "_coerce_seconds",
        "_looks_like_segment",
        "_json_array_candidates",
    )

    def test_neither_backend_reimplements_decoding(self) -> None:
        import importlib

        for module_name in ("audio_transcribe_whisper", "audio_transcribe_vibevoice"):
            module = importlib.import_module(module_name)
            for leaked in self.PRIVATE_DECODERS:
                with self.subTest(module=module_name, function=leaked):
                    self.assertFalse(
                        hasattr(module, leaked),
                        f"{module_name} still exposes {leaked}",
                    )


if __name__ == "__main__":
    unittest.main()
