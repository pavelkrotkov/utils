#!/usr/bin/env python3
# /// script
# dependencies = [
#   "beautifulsoup4",
#   "lxml",
#   "requests",
# ]
# ///
"""Offline evaluation harness for the TIDAL matching pipeline.

Replays cached candidates from a truth file through the shared search driver,
replays auto-selection, and compares against ground-truth choices.

No API calls are made — everything runs from cached data in the truth file.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Dict, List, Optional

from tidal_pipeline.client import CachedSearchBackend
from tidal_pipeline.match import (
    choose_auto_candidate,
    load_truth_records,
    load_weights,
    search_candidates_for_album,
    summarize_review_records,
)
from tidal_pipeline.albums import QueryCandidate
from tidal_pipeline.truth import TruthRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cached_query_candidates(record: TruthRecord) -> List[QueryCandidate]:
    """Rebuild the selected query list persisted in a truth record."""
    if record.query_candidates:
        return list(record.query_candidates)

    if record.queries:
        return [QueryCandidate(template="cached", query=q) for q in record.queries if q]

    seen: set[str] = set()
    fallback: List[QueryCandidate] = []
    for candidate in record.candidates:
        for query in candidate.queries:
            if query and query not in seen:
                seen.add(query)
                fallback.append(QueryCandidate(template="cached", query=query))
    return fallback


# ---------------------------------------------------------------------------
# Per-record evaluation
# ---------------------------------------------------------------------------


class RecordResult:
    """Evaluation outcome for a single truth record."""

    __slots__ = (
        "record_id",
        "title",
        "ground_truth_id",
        "ground_truth_status",
        "original_score",
        "new_score",
        "new_rank",
        "candidate_count",
        "top_id",
        "top_score",
        "auto_id",
        "auto_reason",
        "top1_correct",
        "auto_correct",
    )

    def __init__(
        self,
        record_id: str,
        title: str,
        ground_truth_id: str,
        ground_truth_status: str,
        original_score: float,
        new_score: float,
        new_rank: int,
        candidate_count: int,
        top_id: str,
        top_score: float,
        auto_id: Optional[str],
        auto_reason: str,
    ):
        self.record_id = record_id
        self.title = title
        self.ground_truth_id = ground_truth_id
        self.ground_truth_status = ground_truth_status
        self.original_score = original_score
        self.new_score = new_score
        self.new_rank = new_rank
        self.candidate_count = candidate_count
        self.top_id = top_id
        self.top_score = top_score
        self.auto_id = auto_id
        self.auto_reason = auto_reason
        self.top1_correct = top_id == ground_truth_id
        self.auto_correct = (auto_id == ground_truth_id) if auto_id else False


def evaluate_record(
    record: TruthRecord,
    weights: Dict[str, float],
    score_threshold: float,
    recent_year: int,
    recent_threshold: float,
) -> Optional[RecordResult]:
    """Evaluate a single truth record. Returns None if not evaluable."""
    status = record.choice.status
    if status not in {"selected", "auto_selected"}:
        return None

    ground_truth_id = record.selected_tidal_id
    if not ground_truth_id:
        return None

    raw_candidates = record.candidates
    if not raw_candidates:
        return None

    backend = CachedSearchBackend([record.to_dict()])
    rescored = search_candidates_for_album(
        client=backend,
        album=record.album,
        weights=weights,
        selected_queries=cached_query_candidates(record),
        limit=max(1, len(raw_candidates)),
        sleep_seconds=0,
    )

    # Find the ground-truth candidate in the re-scored ordering
    new_rank = 0
    new_score = 0.0
    for i, c in enumerate(rescored):
        if c.id == ground_truth_id:
            new_rank = i + 1
            new_score = c.score
            break

    # Original score from truth record
    original_score = 0.0
    if record.chosen:
        original_score = record.chosen.score

    # Auto-selection replay
    auto_candidate, auto_reason = choose_auto_candidate(
        rescored,
        score_threshold,
        recent_year,
        recent_threshold,
    )
    auto_id = auto_candidate.id if auto_candidate else None

    top = rescored[0] if rescored else None

    return RecordResult(
        record_id=record.record_id,
        title=record.album.title,
        ground_truth_id=ground_truth_id,
        ground_truth_status=status,
        original_score=original_score,
        new_score=new_score,
        new_rank=new_rank,
        candidate_count=len(rescored),
        top_id=top.id if top else "",
        top_score=top.score if top else 0.0,
        auto_id=auto_id,
        auto_reason=auto_reason,
    )


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


class EvalReport:
    """Aggregate evaluation metrics."""

    def __init__(self, results: List[RecordResult]):
        self.results = results
        self.n = len(results)

    @property
    def precision_at_1(self) -> float:
        if not self.n:
            return 0.0
        return sum(1 for r in self.results if r.top1_correct) / self.n

    @property
    def auto_select_coverage(self) -> float:
        if not self.n:
            return 0.0
        return sum(1 for r in self.results if r.auto_id is not None) / self.n

    @property
    def auto_select_accuracy(self) -> float:
        fired = [r for r in self.results if r.auto_id is not None]
        if not fired:
            return 0.0
        return sum(1 for r in fired if r.auto_correct) / len(fired)

    @property
    def auto_select_recall(self) -> float:
        """Fraction of all evaluable records correctly auto-selected."""
        if not self.n:
            return 0.0
        return sum(1 for r in self.results if r.auto_correct) / self.n

    @property
    def mrr(self) -> float:
        if not self.n:
            return 0.0
        reciprocals = []
        for r in self.results:
            if r.new_rank > 0:
                reciprocals.append(1.0 / r.new_rank)
            else:
                reciprocals.append(0.0)
        return statistics.mean(reciprocals)

    @property
    def correct_scores(self) -> List[float]:
        return [r.new_score for r in self.results if r.new_rank > 0]

    @property
    def regressions(self) -> List[RecordResult]:
        """Records where the correct candidate dropped from rank 1."""
        return [r for r in self.results if not r.top1_correct and r.new_rank > 0]

    @property
    def lost(self) -> List[RecordResult]:
        """Records where the correct candidate is no longer in candidates."""
        return [r for r in self.results if r.new_rank == 0]

    def score_stats(self) -> Dict[str, float]:
        scores = self.correct_scores
        if not scores:
            return {"min": 0, "median": 0, "mean": 0, "max": 0}
        return {
            "min": min(scores),
            "median": statistics.median(scores),
            "mean": statistics.mean(scores),
            "max": max(scores),
        }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_report(report: EvalReport, verbose: bool = False) -> None:
    stats = report.score_stats()

    print(f"\n{'=' * 60}")
    print("  TIDAL Matching Evaluation Report")
    print(f"{'=' * 60}")
    print(f"  Records evaluated:       {report.n}")
    print(
        f"  Precision@1:             {report.precision_at_1:.1%}  "
        f"({sum(1 for r in report.results if r.top1_correct)}/{report.n})"
    )
    print(f"  MRR:                     {report.mrr:.4f}")
    print(
        f"  Auto-select coverage:    {report.auto_select_coverage:.1%}  "
        f"({sum(1 for r in report.results if r.auto_id is not None)}/{report.n})"
    )
    print(f"  Auto-select accuracy:    {report.auto_select_accuracy:.1%}")
    print(
        f"  Auto-select recall:      {report.auto_select_recall:.1%}  "
        f"({sum(1 for r in report.results if r.auto_correct)}/{report.n})"
    )
    print(f"{'─' * 60}")
    print("  Score distribution (correct candidate):")
    print(f"    min:    {stats['min']:.4f}")
    print(f"    median: {stats['median']:.4f}")
    print(f"    mean:   {stats['mean']:.4f}")
    print(f"    max:    {stats['max']:.4f}")

    regressions = report.regressions
    lost = report.lost
    if regressions or lost:
        print(f"{'─' * 60}")
        print(f"  Regressions:  {len(regressions)}  |  Lost:  {len(lost)}")
        for r in regressions:
            delta = r.new_score - r.original_score
            sign = "+" if delta >= 0 else ""
            print(
                f"    rank {r.new_rank:>3}  score {r.new_score:.3f} ({sign}{delta:.3f})  {r.title}"
            )
        for r in lost:
            print(f"    LOST  (was {r.original_score:.3f})  {r.title}")
    else:
        print(f"{'─' * 60}")
        print("  No regressions.")

    print(f"{'=' * 60}\n")

    if verbose:
        print_verbose(report)


def print_verbose(report: EvalReport) -> None:
    print(f"{'─' * 80}")
    print("  Per-album detail")
    print(f"{'─' * 80}")
    for r in sorted(report.results, key=lambda x: x.new_rank if x.new_rank > 0 else 9999):
        ok = "ok" if r.top1_correct else "MISS"
        auto = "auto" if r.auto_correct else ("auto-wrong" if r.auto_id else "no-auto")
        delta = r.new_score - r.original_score
        sign = "+" if delta >= 0 else ""
        print(
            f"  [{ok:>4}] rank={r.new_rank:<3} "
            f"score={r.new_score:.3f} ({sign}{delta:.3f}) "
            f"[{auto}] {r.title}"
        )
    print()


def print_json_report(report: EvalReport) -> None:
    data = {
        "records_evaluated": report.n,
        "precision_at_1": round(report.precision_at_1, 4),
        "mrr": round(report.mrr, 4),
        "auto_select_coverage": round(report.auto_select_coverage, 4),
        "auto_select_accuracy": round(report.auto_select_accuracy, 4),
        "auto_select_recall": round(report.auto_select_recall, 4),
        "score_stats": {k: round(v, 4) for k, v in report.score_stats().items()},
        "regression_count": len(report.regressions),
        "lost_count": len(report.lost),
        "regressions": [
            {
                "record_id": r.record_id,
                "title": r.title,
                "new_rank": r.new_rank,
                "new_score": round(r.new_score, 4),
                "original_score": round(r.original_score, 4),
            }
            for r in report.regressions
        ],
        "per_album": [
            {
                "record_id": r.record_id,
                "title": r.title,
                "ground_truth_id": r.ground_truth_id,
                "top1_correct": r.top1_correct,
                "auto_correct": r.auto_correct,
                "new_rank": r.new_rank,
                "new_score": round(r.new_score, 4),
                "original_score": round(r.original_score, 4),
                "candidate_count": r.candidate_count,
            }
            for r in report.results
        ],
    }
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Offline evaluation of TIDAL matching quality against a truth file.",
    )
    p.add_argument("truth_file", type=Path, help="Path to truth JSON file")
    p.add_argument(
        "--weights",
        type=Path,
        default=None,
        help="Path to scoring weights JSON (uses defaults if omitted)",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Auto-select score threshold (default: 0.85)",
    )
    p.add_argument(
        "--recent-year",
        type=int,
        default=2025,
        help="Recent release year for relaxed threshold (default: 2025)",
    )
    p.add_argument(
        "--recent-threshold",
        type=float,
        default=0.50,
        help="Relaxed threshold for recent releases (default: 0.50)",
    )
    p.add_argument(
        "--min-precision",
        type=float,
        default=None,
        help="Exit non-zero if precision@1 drops below this value",
    )
    p.add_argument(
        "--min-recall",
        type=float,
        default=None,
        help="Exit non-zero if auto-select recall drops below this value",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Show per-album detail")
    p.add_argument("--json", action="store_true", help="Output as JSON instead of text")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    records = load_truth_records(args.truth_file)
    weights = load_weights(args.weights)
    summary = summarize_review_records(records)

    if not args.json:
        total = sum(summary.values())
        print(f"Loaded {total} truth records from {args.truth_file}")
        print(
            f"  auto_selected={summary['auto_selected']}  selected={summary['selected']}  "
            f"needs_review={summary['needs_review']}  skip={summary['skip']}  none={summary['none']}"
        )
        print("Evaluating records with status in {selected, auto_selected}...")

    results: List[RecordResult] = []
    for record in records:
        result = evaluate_record(
            record,
            weights,
            score_threshold=args.threshold,
            recent_year=args.recent_year,
            recent_threshold=args.recent_threshold,
        )
        if result is not None:
            results.append(result)

    report = EvalReport(results)

    if args.json:
        print_json_report(report)
    else:
        print_report(report, verbose=args.verbose)

    # Gate: exit non-zero if quality thresholds are breached
    exit_code = 0
    if args.min_precision is not None and report.precision_at_1 < args.min_precision:
        if not args.json:
            print(f"FAIL: precision@1 {report.precision_at_1:.4f} < {args.min_precision:.4f}")
        exit_code = 1
    if args.min_recall is not None and report.auto_select_recall < args.min_recall:
        if not args.json:
            print(
                f"FAIL: auto-select recall {report.auto_select_recall:.4f} < {args.min_recall:.4f}"
            )
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
