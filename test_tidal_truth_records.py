#!/usr/bin/env python3
"""Tests for persisted TIDAL truth-record models."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tidal_pipeline.models import Choice, TruthRecord


class TruthRecordTests(unittest.TestCase):
    def test_sample_truth_record_round_trips(self) -> None:
        sample = {
            "record_id": "sample.md|7|Example Album",
            "source": {
                "file": "sample.md",
                "line": 7,
                "raw": "Example Album",
                "subsection": "## Example Album",
                "context": {},
            },
            "album": {
                "title": "Example Album",
                "composers": ["Composer"],
                "performers": ["Performer"],
                "ensembles": [],
                "conductor": "",
                "label": "Label",
                "year": "2025",
                "works": ["Example Album"],
                "instruments": [],
            },
            "queries": ["Example Album"],
            "query_candidates": [{"template": "title", "query": "Example Album"}],
            "candidates": [
                {
                    "id": "123",
                    "title": "Example Album",
                    "artists": ["Performer"],
                    "release_date": "2025-01-01",
                    "copyright": "Label",
                    "track_count": 10,
                    "score": 0.95,
                    "features": {"title": 1.0},
                    "queries": ["Example Album"],
                    "details_fetched": True,
                }
            ],
            "top_candidates": [
                {
                    "id": "123",
                    "title": "Example Album",
                    "artists": ["Performer"],
                    "release_date": "2025-01-01",
                    "copyright": "Label",
                    "track_count": 10,
                    "score": 0.95,
                    "features": {"title": 1.0},
                    "queries": ["Example Album"],
                    "details_fetched": True,
                }
            ],
            "choice": {
                "status": "selected",
                "tidal_id": "123",
                "selected_at": "2026-01-02T03:04:05",
                "manual": False,
            },
            "chosen": {
                "id": "123",
                "title": "Example Album",
                "artists": ["Performer"],
                "release_date": "2025-01-01",
                "copyright": "Label",
                "track_count": 10,
                "score": 0.95,
                "features": {"title": 1.0},
                "queries": ["Example Album"],
                "details_fetched": True,
            },
            "review": {
                "mode": "test",
                "top_score": 0.95,
                "top_release_year": "2025",
                "candidate_count": 1,
                "auto_reason": "",
                "auto_threshold": 0.85,
                "recent_year": 2025,
                "recent_threshold": 0.5,
            },
            "meta": {
                "generated_at": "2026-01-02T03:04:05",
                "weights": {"title": 0.35},
                "limit": 10,
                "max_queries": 12,
            },
        }

        record = TruthRecord.from_dict(sample)

        self.assertIsInstance(record.choice, Choice)
        self.assertEqual(record.album.title, "Example Album")
        self.assertEqual(record.choice.tidal_id, "123")
        self.assertEqual(record.source_line, 7)
        self.assertEqual(record.to_dict(), sample)

    def test_existing_truth_file_round_trips_byte_identically(self) -> None:
        path = Path("album-debug/best2025.truth.json")
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        records = [TruthRecord.from_dict(item) for item in data]
        rendered = (
            json.dumps(
                [record.to_dict() for record in records],
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )

        self.assertEqual(rendered, original)


if __name__ == "__main__":
    unittest.main()
