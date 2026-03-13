#!/usr/bin/env python3
# /// script
# dependencies = [
#   "beautifulsoup4",
#   "lxml",
# ]
# ///
"""Parse Gramophone-style MHTML/Markdown into structured JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tidal_pipeline.parse import parse_file_to_entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse Gramophone-style MHTML/Markdown into structured JSON.",
    )
    parser.add_argument("input_path", type=Path, help="Input .mhtml or .md file")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file (defaults to <input>.albums.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input_path.exists():
        raise SystemExit(f"Input file not found: {args.input_path}")

    entries = parse_file_to_entries(args.input_path)
    output_path = args.output or args.input_path.with_suffix(".albums.json")
    output_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(entries)} albums to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
