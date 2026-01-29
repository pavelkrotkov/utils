#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
#   "beautifulsoup4",
#   "lxml",
# ]
# ///
"""Helper script to test and analyze TIDAL matching results.

Usage:
    pipx run ./test_matching.py test_2.md          # Quick test on subset
    pipx run ./test_matching.py full.mhtml         # Full test
    pipx run ./test_matching.py test_2.md --compare baseline.log  # Compare to baseline
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def run_dry_run(input_file: Path, debug: bool = False) -> str:
    """Run dry run and return output."""
    cmd = ["pipx", "run", "./tidal_import_page_to_playlist.py", str(input_file), "--dry-run"]
    if debug:
        cmd.append("--debug")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return result.stdout + result.stderr


def parse_results(output: str) -> dict:
    """Parse dry run output into structured results."""
    results = {
        "total_candidates": 0,
        "matches": [],
        "no_matches": [],
        "by_score": {},
    }

    # Extract candidate count
    m = re.search(r"Found (\d+) candidates", output)
    if m:
        results["total_candidates"] = int(m.group(1))

    # Extract matches with scores
    for match in re.finditer(
        r"MATCH: '([^']+)'.*?-> ID: (\d+) \(Score: ([\d.]+)\)", output, re.DOTALL
    ):
        title, album_id, score = match.groups()
        score = float(score)
        results["matches"].append(
            {
                "title": title,
                "id": album_id,
                "score": score,
            }
        )

        # Group by score bucket
        bucket = f"{score:.1f}"
        if bucket not in results["by_score"]:
            results["by_score"][bucket] = []
        results["by_score"][bucket].append(title)

    # Extract no matches
    for match in re.finditer(r"NO MATCH: '([^']+)'", output):
        results["no_matches"].append(match.group(1))

    return results


def print_summary(results: dict, label: str = ""):
    """Print summary of results."""
    if label:
        print(f"\n=== {label} ===")

    print(f"Total candidates: {results['total_candidates']}")
    print(f"Matched: {len(results['matches'])}")
    print(f"No Match: {len(results['no_matches'])}")

    if results["no_matches"]:
        print("\nNO MATCH albums:")
        for title in results["no_matches"]:
            print(f"  - {title[:70]}")

    # Score distribution
    print("\nScore distribution:")
    for score in sorted(results["by_score"].keys(), reverse=True):
        count = len(results["by_score"][score])
        print(f"  {score}: {count} albums")

    # Low scores (< 0.7)
    low_scores = [(m["title"], m["score"]) for m in results["matches"] if m["score"] < 0.7]
    if low_scores:
        print(f"\nLow score matches (< 0.7): {len(low_scores)}")
        for title, score in sorted(low_scores, key=lambda x: x[1]):
            print(f"  {score:.2f}: {title[:60]}")


def compare_results(current: dict, baseline: dict):
    """Compare current results to baseline."""
    print("\n=== COMPARISON ===")

    # NO MATCH changes
    current_no = set(current["no_matches"])
    baseline_no = set(baseline["no_matches"])

    fixed = baseline_no - current_no
    regressed = current_no - baseline_no

    if fixed:
        print(f"\nFIXED ({len(fixed)} albums now match):")
        for title in fixed:
            print(f"  + {title[:70]}")

    if regressed:
        print(f"\nREGRESSED ({len(regressed)} albums no longer match):")
        for title in regressed:
            print(f"  - {title[:70]}")

    # Score changes
    current_scores = {m["title"]: m["score"] for m in current["matches"]}
    baseline_scores = {m["title"]: m["score"] for m in baseline["matches"]}

    improved = []
    worsened = []

    for title in set(current_scores.keys()) & set(baseline_scores.keys()):
        diff = current_scores[title] - baseline_scores[title]
        if diff > 0.1:
            improved.append((title, baseline_scores[title], current_scores[title]))
        elif diff < -0.1:
            worsened.append((title, baseline_scores[title], current_scores[title]))

    if improved:
        print(f"\nIMPROVED SCORES ({len(improved)}):")
        for title, old, new in sorted(improved, key=lambda x: x[2] - x[1], reverse=True)[:10]:
            print(f"  {old:.2f} -> {new:.2f}: {title[:50]}")

    if worsened:
        print(f"\nWORSENED SCORES ({len(worsened)}):")
        for title, old, new in sorted(worsened, key=lambda x: x[2] - x[1])[:10]:
            print(f"  {old:.2f} -> {new:.2f}: {title[:50]}")

    # Summary
    print("\n=== SUMMARY ===")
    print(
        f"NO MATCH: {len(baseline_no)} -> {len(current_no)} ({len(current_no) - len(baseline_no):+d})"
    )
    print(f"Fixed: {len(fixed)}, Regressed: {len(regressed)}")


def main():
    parser = argparse.ArgumentParser(description="Test TIDAL matching results")
    parser.add_argument("input_file", type=Path, help="Input file to test")
    parser.add_argument("--compare", type=Path, help="Baseline log to compare against")
    parser.add_argument("--debug", action="store_true", help="Show debug output")
    parser.add_argument("--save", type=Path, help="Save output to file")
    args = parser.parse_args()

    if not args.input_file.exists():
        print(f"Error: {args.input_file} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Running dry run on {args.input_file}...")
    output = run_dry_run(args.input_file, args.debug)

    if args.save:
        args.save.write_text(output)
        print(f"Saved output to {args.save}")

    current = parse_results(output)
    print_summary(current, "CURRENT RESULTS")

    if args.compare:
        if args.compare.exists():
            baseline_output = args.compare.read_text()
            baseline = parse_results(baseline_output)
            compare_results(current, baseline)
        else:
            print(f"Warning: Baseline file {args.compare} not found", file=sys.stderr)


if __name__ == "__main__":
    main()
