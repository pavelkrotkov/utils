#!/usr/bin/env python3
import unittest

import audio_transcribe_whisper as whisper


class MergeAsrTurnsTest(unittest.TestCase):
    def test_labels_style_groups_segments_and_remaps_names(self) -> None:
        lines = whisper.merge_asr_turns(
            [
                (0.0, 1.0, "Hello"),
                (1.0, 2.0, "there."),
                (2.0, 3.0, "Hi."),
                (3.0, 4.0, "Again."),
            ],
            [
                (0.0, 2.0, "speaker-a"),
                (2.0, 3.0, "speaker-b"),
                (3.0, 4.0, "speaker-a"),
            ],
            "labels",
            ["Alice", "Bob"],
        )

        self.assertEqual(
            lines,
            [
                "Alice: Hello there.",
                "Bob: Hi.",
                "Alice: Again.",
            ],
        )

    def test_breaks_style_emits_speaker_change_markers(self) -> None:
        lines = whisper.merge_asr_turns(
            [
                (0.0, 1.0, "Hello"),
                (1.0, 2.0, "there."),
                (2.0, 3.0, "Hi."),
            ],
            [
                (0.0, 2.0, "speaker-a"),
                (2.0, 3.0, "speaker-b"),
            ],
            "breaks",
            ["Alice", "Bob"],
        )

        self.assertEqual(
            lines,
            [
                "--- speaker change ---",
                "Hello there.",
                "--- speaker change ---",
                "Hi.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
