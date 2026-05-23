#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""Apply chosen TIDAL album links from a truth JSON file back into markdown sections."""

from __future__ import annotations

import argparse
from pathlib import Path

from tidal_pipeline.links import apply_updates, load_updates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insert chosen TIDAL album links into a markdown source file.",
    )
    parser.add_argument("markdown_path", type=Path, help="Markdown file to update")
    parser.add_argument("truth_path", type=Path, help="Truth JSON from tidal_match_from_json.py")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.markdown_path.exists():
        raise SystemExit(f"Markdown file not found: {args.markdown_path}")
    if not args.truth_path.exists():
        raise SystemExit(f"Truth JSON not found: {args.truth_path}")

    updates = load_updates(args.truth_path)
    if not updates:
        raise SystemExit("No selected TIDAL matches found in truth JSON.")

    lines = args.markdown_path.read_text(encoding="utf-8").splitlines()
    new_lines, changed = apply_updates(lines, updates)
    if args.dry_run:
        print(f"Would apply {changed} TIDAL link updates from {args.truth_path}")
        return 0

    args.markdown_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"Applied {changed} TIDAL link updates to {args.markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
