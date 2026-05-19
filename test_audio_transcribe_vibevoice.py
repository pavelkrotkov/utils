import unittest

from audio_transcribe_vibevoice import _extract_raw_segments


class VibeVoiceSegmentExtractionTests(unittest.TestCase):
    def test_extracts_explicit_nested_segments(self) -> None:
        data = {
            "results": {
                "metadata": {"ids": [1, 2, 3]},
                "segments": [{"start": 1.0, "end": 2.0, "text": "hello"}],
            }
        }

        self.assertEqual(
            _extract_raw_segments(data),
            [{"start": 1.0, "end": 2.0, "text": "hello"}],
        )

    def test_ignores_unrelated_lists_under_segment_keys(self) -> None:
        data = {
            "results": {
                "segments": [{"id": 123, "score": 0.9}],
                "text": "fallback transcript",
            }
        }

        self.assertEqual(_extract_raw_segments(data), [])


if __name__ == "__main__":
    unittest.main()
