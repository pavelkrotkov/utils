#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

import audio_transcribe_whisper as whisper


class RunWhisperCommandTest(unittest.TestCase):
    def test_default_command_disables_context_carryover(self) -> None:
        cmd, _ = self.capture_run_whisper_command()

        self.assert_flag_value(cmd, "-mc", "0")

    def test_explicit_max_context_can_restore_whisper_default(self) -> None:
        cmd, _ = self.capture_run_whisper_command(max_context=-1)

        self.assert_flag_value(cmd, "-mc", "-1")

    def test_required_whisper_flags_are_preserved(self) -> None:
        cmd, output_stem = self.capture_run_whisper_command()

        self.assert_flag_value(cmd, "-m", "/tmp/model.bin")
        self.assert_flag_value(cmd, "-f", "/tmp/audio.wav")
        self.assert_flag_value(cmd, "-t", "4")
        self.assert_flag_value(cmd, "-of", output_stem)
        self.assert_flag_value(cmd, "-l", "en")
        self.assertIn("-oj", cmd)

    def capture_run_whisper_command(self, **kwargs: object) -> tuple[list[str], str]:
        captured_commands: list[list[str]] = []
        original_run_with_progress = whisper.run_with_progress

        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "whisper.json"
            output_stem = str(json_path.with_suffix(""))
            json_path.write_text("{}", encoding="utf-8")

            def fake_run_with_progress(
                cmd: list[str], *args: object, **kwargs: object
            ) -> list[str]:
                del args, kwargs
                captured_commands.append(list(cmd))
                return []

            whisper.run_with_progress = fake_run_with_progress
            try:
                run_kwargs = {
                    "audio_path": Path("/tmp/audio.wav"),
                    "json_path": json_path,
                    "whisper_bin": "whisper-cli",
                    "model_path": Path("/tmp/model.bin"),
                    "threads": 4,
                    "language": "en",
                    "verbose": False,
                }
                run_kwargs.update(kwargs)
                whisper.run_whisper(**run_kwargs)
            finally:
                whisper.run_with_progress = original_run_with_progress

        self.assertEqual(1, len(captured_commands))
        return captured_commands[0], output_stem

    def assert_flag_value(self, cmd: list[str], flag: str, value: str) -> None:
        self.assertIn(flag, cmd)
        flag_index = cmd.index(flag)
        self.assertLess(flag_index + 1, len(cmd))
        self.assertEqual(value, cmd[flag_index + 1])


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
