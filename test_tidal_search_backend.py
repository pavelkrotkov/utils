#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Unit tests for TIDAL search backend adapters."""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional

from tidal_pipeline.client import (
    AlbumDetail,
    AlbumHit,
    CachedSearchBackend,
    SearchBackend,
    TidalClient,
)
from tidal_pipeline.match import score_manual_candidate, search_candidates_for_album
from tidal_pipeline.models import AlbumInput, QueryCandidate


class HandBuiltBackend(SearchBackend):
    def __init__(
        self,
        hits_by_query: Dict[str, List[AlbumHit]],
        details_by_id: Optional[Dict[str, AlbumDetail]] = None,
    ) -> None:
        self.hits_by_query = hits_by_query
        self.details_by_id = details_by_id or {}

    def search_albums(self, query: str, limit: int = 5) -> List[AlbumHit]:
        return self.hits_by_query.get(query, [])[:limit]

    def get_album_details(self, album_id: str) -> Optional[AlbumDetail]:
        return self.details_by_id.get(album_id)


class SearchBackendTest(unittest.TestCase):
    def test_tidal_client_satisfies_search_backend_protocol(self) -> None:
        self.assertIsInstance(TidalClient("token", "US"), SearchBackend)

    def test_search_candidates_scores_and_ranks_hand_built_backend(self) -> None:
        first = AlbumHit(
            id="first",
            title="Alpha",
            artists=[],
            release_date="2025-01-01",
            copyright="",
        )
        second = AlbumHit(
            id="second",
            title="Alpha",
            artists=[],
            release_date="2025-01-01",
            copyright="",
        )
        backend = HandBuiltBackend({"q1": [first, second], "q2": [second]})
        album = AlbumInput(title="Alpha")
        weights = {
            "title": 1.0,
            "composer": 0.0,
            "performer": 0.0,
            "ensemble": 0.0,
            "conductor": 0.0,
            "instrument": 0.0,
            "label": 0.0,
            "year": 0.0,
        }

        candidates = search_candidates_for_album(
            client=backend,
            album=album,
            weights=weights,
            selected_queries=[
                QueryCandidate(template="test", query="q1"),
                QueryCandidate(template="test", query="q2"),
            ],
            limit=5,
            sleep_seconds=0,
        )

        self.assertEqual([candidate.id for candidate in candidates], ["second", "first"])
        self.assertEqual(candidates[0].queries, ["q1", "q2"])
        self.assertEqual(candidates[1].queries, ["q1"])
        self.assertAlmostEqual(candidates[0].score, candidates[1].score)
        self.assertGreater(candidates[0].score, 0.0)

    def test_score_manual_candidate_fetches_details_and_scores_backend_result(self) -> None:
        backend = HandBuiltBackend(
            {},
            {
                "manual": AlbumDetail(
                    id="manual",
                    title="Manual Album",
                    artists=["Soloist"],
                    release_date="2025-01-01",
                    copyright="Manual Label",
                    track_count=9,
                )
            },
        )
        album = AlbumInput(title="Manual Album", performers=["Soloist"], label="Manual Label")
        weights = {
            "title": 1.0,
            "composer": 0.0,
            "performer": 1.0,
            "ensemble": 0.0,
            "conductor": 0.0,
            "instrument": 0.0,
            "label": 1.0,
            "year": 0.0,
        }

        candidate = score_manual_candidate(backend, album, "manual", weights)

        self.assertEqual(candidate.id, "manual")
        self.assertEqual(candidate.title, "Manual Album")
        self.assertEqual(candidate.artists, ["Soloist"])
        self.assertEqual(candidate.track_count, 9)
        self.assertTrue(candidate.details_fetched)
        self.assertGreater(candidate.score, 0.0)
        self.assertGreater(candidate.features["title"], 0.0)

    def test_cached_search_backend_returns_hits_from_cached_candidate_queries(self) -> None:
        backend = CachedSearchBackend(
            [
                {
                    "candidates": [
                        {
                            "id": "cached",
                            "title": "Cached Album",
                            "artists": ["Performer"],
                            "release_date": "2025-01-01",
                            "copyright": "Label",
                            "track_count": 12,
                            "queries": ["cached query"],
                        }
                    ]
                }
            ]
        )

        hits = backend.search_albums("cached query", limit=5)
        details = backend.get_album_details("cached")

        self.assertEqual([hit.id for hit in hits], ["cached"])
        self.assertIsNotNone(details)
        self.assertEqual(details.track_count if details else None, 12)


if __name__ == "__main__":
    unittest.main()
