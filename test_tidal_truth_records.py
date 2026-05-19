#!/usr/bin/env python3
"""Tests for persisted TIDAL truth-record models."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tidal_pipeline.albums import AlbumInput, Candidate, QueryCandidate
from tidal_pipeline.truth import Choice, TruthRecord


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

    def test_from_match_result_uses_explicit_top_keyword(self) -> None:
        album = AlbumInput(title="Example Album")
        candidates = [
            Candidate(
                id=str(index),
                title=f"Candidate {index}",
                artists=[],
                release_date="",
                copyright="",
                score=1.0 - index / 10,
                features={},
            )
            for index in range(3)
        ]

        record = TruthRecord.from_match_result(
            album=album,
            record_id="record",
            ordered=candidates,
            selected_queries=[QueryCandidate(template="title", query="Example Album")],
            chosen=candidates[0],
            choice=Choice(status="selected", tidal_id="0"),
            top=2,
            review={"mode": "test"},
            meta={},
        )

        self.assertEqual([candidate.id for candidate in record.top_candidates], ["0", "1"])
        self.assertEqual(record.review, {"mode": "test"})

    def test_feature_parse_error_names_bad_key(self) -> None:
        sample = {
            "record_id": "sample",
            "candidates": [
                {
                    "id": "123",
                    "title": "Example Album",
                    "artists": [],
                    "release_date": "",
                    "copyright": "",
                    "score": 0.5,
                    "features": {"title": "not-a-number"},
                }
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            r"features\['title'\] must be a number, got 'not-a-number'",
        ):
            TruthRecord.from_dict(sample)


if __name__ == "__main__":
    unittest.main()
