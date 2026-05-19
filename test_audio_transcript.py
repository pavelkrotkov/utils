import unittest

from audio_transcript import (
    TranscriptSegment,
    emit_diarized_breaks,
    emit_diarized_txt,
    emit_srt,
    emit_transcript,
    emit_txt,
    emit_vtt,
    remap_speakers,
)


class TranscriptEmitterTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(emit_txt([]), "")
        self.assertEqual(emit_srt([]), "")
        self.assertEqual(emit_vtt([]), "WEBVTT")
        self.assertEqual(emit_diarized_txt([]), "")

    def test_single_segment_txt(self) -> None:
        segments = [TranscriptSegment(1.0, 2.0, " hello ")]

        self.assertEqual(emit_txt(segments), "hello")

    def test_multi_line_srt_timestamping(self) -> None:
        segments = [TranscriptSegment(1.234, 3661.005, "First line\nSecond line")]

        self.assertEqual(
            emit_srt(segments),
            "1\n00:00:01,234 --> 01:01:01,005\nFirst line\nSecond line",
        )

    def test_vtt_cue_formatting(self) -> None:
        segments = [
            TranscriptSegment(0.0, 1.5, "Hello"),
            TranscriptSegment(61.25, 62.75, "World"),
        ]

        self.assertEqual(
            emit_vtt(segments),
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.500\n"
            "Hello\n\n"
            "00:01:01.250 --> 00:01:02.750\n"
            "World",
        )

    def test_diarized_txt_groups_speaker_labels(self) -> None:
        segments = [
            TranscriptSegment(0.0, 1.0, "Hello", "SPEAKER_00"),
            TranscriptSegment(1.0, 2.0, "again", "SPEAKER_00"),
            TranscriptSegment(2.0, 3.0, "Hi", "SPEAKER_01"),
        ]

        self.assertEqual(
            emit_diarized_txt(segments),
            "SPEAKER_00: Hello again\nSPEAKER_01: Hi",
        )

    def test_speaker_name_remapping(self) -> None:
        segments = [
            TranscriptSegment(0.0, 1.0, "Hello", "speaker-a"),
            TranscriptSegment(1.0, 2.0, "Hi", "speaker-b"),
        ]

        remapped = remap_speakers(segments, ["Alice", "Bob"])

        self.assertEqual(
            emit_diarized_txt(remapped),
            "Alice: Hello\nBob: Hi",
        )

    def test_diarized_txt_maps_speaker_names_without_pre_remap(self) -> None:
        segments = [
            TranscriptSegment(0.0, 1.0, "Hello", "SPEAKER_00"),
            TranscriptSegment(1.0, 2.0, "Hi", "SPEAKER_01"),
        ]

        self.assertEqual(
            emit_transcript(segments, "diarized-txt", ["Alice", "Bob"]),
            "Alice: Hello\nBob: Hi",
        )

    def test_all_scripts_share_srt_vtt_emitters(self) -> None:
        segments = [TranscriptSegment(0.0, 1.0, "Same text")]

        self.assertEqual(emit_transcript(segments, "srt"), emit_srt(segments))
        self.assertEqual(emit_transcript(segments, "vtt"), emit_vtt(segments))

    def test_diarized_breaks_separates_consecutive_speakers(self) -> None:
        segments = [
            TranscriptSegment(0.0, 1.0, "Hello", "SPEAKER_00"),
            TranscriptSegment(1.0, 2.0, "again", "SPEAKER_00"),
            TranscriptSegment(2.0, 3.0, "Hi there", "SPEAKER_01"),
            TranscriptSegment(3.0, 4.0, "Back", "SPEAKER_00"),
        ]

        self.assertEqual(
            emit_diarized_breaks(segments),
            "Hello again\n--- speaker change ---\nHi there\n--- speaker change ---\nBack",
        )
        self.assertEqual(
            emit_transcript(segments, "diarized-breaks"),
            emit_diarized_breaks(segments),
        )


if __name__ == "__main__":
    unittest.main()
