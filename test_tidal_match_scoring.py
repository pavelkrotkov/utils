#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
import unittest

from tidal_pipeline.client import AlbumHit
from tidal_pipeline.match import apply_penalties, base_score, extract_features, score_candidate
from tidal_pipeline.models import AlbumInput


class TidalMatchScoringTest(unittest.TestCase):
    def test_extract_features_curated_full_match(self) -> None:
        album = AlbumInput(
            title="Violin Concerto",
            composers=["Elgar"],
            performers=["Hilary Hahn"],
            ensembles=["London Symphony Orchestra"],
            conductor="Antonio Pappano",
            label="Decca",
            year="2025",
            instruments=["violin"],
        )
        hit = AlbumHit(
            id="1",
            title="Elgar: Violin Concerto",
            artists=["Hilary Hahn", "London Symphony Orchestra", "Antonio Pappano"],
            release_date="2025-03-01",
            copyright="2025 Decca Music",
        )

        self.assertEqual(
            extract_features(album, hit),
            {
                "title": 1.0,
                "composer": 1.0,
                "performer": 1.0,
                "ensemble": 1.0,
                "conductor": 1.0,
                "instrument": 1.0,
                "label": 1.0,
                "year": 1.0,
            },
        )

    def test_base_score_uses_supplied_weights(self) -> None:
        features = {"title": 0.5, "performer": 0.25, "unknown": 1.0}
        weights = {"title": 2.0, "performer": 4.0}

        self.assertEqual(base_score(features, weights), 2.0)

    def test_apply_penalties_keeps_score_when_label_matched(self) -> None:
        album = AlbumInput(title="Recital", label="Alpha")
        hit = AlbumHit(
            id="1",
            title="Recital",
            artists=[],
            release_date="",
            copyright="Alpha Classics",
        )
        features = {
            "title": 1.0,
            "composer": 0.0,
            "performer": 0.0,
            "ensemble": 0.0,
            "conductor": 0.0,
            "instrument": 0.0,
            "label": 1.0,
            "year": 0.0,
        }

        self.assertEqual(apply_penalties(1.0, album, features, hit), 1.0)

    def test_apply_penalties_penalizes_generic_title_without_composer_match(self) -> None:
        album = AlbumInput(
            title="Symphonies",
            composers=["Mozart"],
            performers=["Hilary Hahn"],
        )
        hit = AlbumHit(
            id="1", title="Symphonies", artists=["Hilary Hahn"], release_date="", copyright=""
        )
        features = {
            "title": 1.0,
            "composer": 0.0,
            "performer": 1.0,
            "ensemble": 0.0,
            "conductor": 0.0,
            "instrument": 0.0,
            "label": 0.0,
            "year": 0.0,
        }

        self.assertEqual(apply_penalties(1.0, album, features, hit), 0.55)

    def test_apply_penalties_penalizes_weak_artist_support(self) -> None:
        album = AlbumInput(title="The Bells", performers=["Jane Smith"])
        hit = AlbumHit(
            id="1", title="The Bells", artists=["Jane Ensemble"], release_date="", copyright=""
        )
        features = {
            "title": 1.0,
            "composer": 0.0,
            "performer": 0.33,
            "ensemble": 0.0,
            "conductor": 0.0,
            "instrument": 0.0,
            "label": 0.0,
            "year": 0.0,
        }

        self.assertEqual(apply_penalties(1.0, album, features, hit), 0.7)

    def test_score_candidate_composes_layers(self) -> None:
        album = AlbumInput(title="Piano Concerto", performers=["Martha Argerich"])
        hit = AlbumHit(
            id="1",
            title="Piano Concerto",
            artists=["Martha Argerich"],
            release_date="",
            copyright="",
        )
        weights = {"title": 0.5, "performer": 0.5}

        score, features = score_candidate(album, hit, weights)

        self.assertEqual(features, extract_features(album, hit))
        self.assertEqual(
            score, apply_penalties(base_score(features, weights), album, features, hit)
        )


if __name__ == "__main__":
    unittest.main()
