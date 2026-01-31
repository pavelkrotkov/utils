#!/usr/bin/env python3
# /// script
# dependencies = [
#   "beautifulsoup4",
#   "lxml",
#   "numpy",
#   "pandas",
#   "requests",
# ]
# ///
"""Fetch FT Markets performance tables for ETF/fund tickers."""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup


USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


@dataclass
class FetchSettings:
    timeout: float
    retries: int
    backoff: float


def get_random_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }


def fetch_soup(url: str, settings: FetchSettings) -> BeautifulSoup:
    last_exc: Exception | None = None
    for attempt in range(1, settings.retries + 1):
        try:
            response = requests.get(url, headers=get_random_headers(), timeout=settings.timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, "lxml")
        except Exception as exc:  # noqa: BLE001 - surface helpful errors with retries
            last_exc = exc
            if attempt < settings.retries:
                time.sleep(settings.backoff * attempt)
    raise RuntimeError(f"Failed to fetch {url}") from last_exc


def frame_from_html(table, transpose: bool = False) -> pd.DataFrame:
    if table is None:
        raise ValueError("table is None")
    rows = [
        [cell.get_text() for cell in row.find_all(["th", "td"])] for row in table.find_all("tr")
    ]
    if not rows:
        raise ValueError("table has no rows")
    if transpose:
        rows = np.transpose(rows)
    return pd.DataFrame(data=rows, columns=rows[0])


def frame_for_symbol(symbol: str, settings: FetchSettings) -> pd.DataFrame:
    performance_url = f"https://markets.ft.com/data/etfs/tearsheet/performance?s={symbol}"
    performance_soup = fetch_soup(performance_url, settings)
    performance_table = performance_soup.find("table")
    if performance_table is None:
        raise ValueError("performance table not found")

    summary_url = f"https://markets.ft.com/data/etfs/tearsheet/summary?s={symbol}"
    summary_soup = fetch_soup(summary_url, settings)
    summary_tables = summary_soup.find_all("table")
    if len(summary_tables) < 2:
        raise ValueError("summary tables not found")

    return pd.concat(
        [
            frame_from_html(performance_table)[:3],
            frame_from_html(summary_tables[0], True).reindex(
                columns=["Morningstar category", "Launch date", "Symbol"]
            ),
            frame_from_html(summary_tables[1], True).reindex(
                columns=["Total net assets", "Net expense ratio"]
            ),
        ],
        axis=1,
    )


def parse_tickers_from_args(values: list[str] | None) -> list[str]:
    if not values:
        return []
    tickers: list[str] = []
    for value in values:
        for token in value.replace(",", " ").split():
            token = token.strip()
            if token:
                tickers.append(token)
    return tickers


def parse_tickers_from_file(path: str) -> list[str]:
    if path == "-":
        content = sys.stdin.read()
    else:
        content = Path(path).read_text(encoding="utf-8")

    tickers: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            row = next(csv.reader([line]))
            token = row[0].strip() if row else ""
        else:
            token = line.split()[0].strip()
        if token.lower() in {"ticker", "symbol"}:
            continue
        if token:
            tickers.append(token)
    return tickers


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def normalize_symbol(ticker: str, suffix: str) -> str:
    ticker = ticker.strip()
    if not ticker:
        return ""
    if ":" in ticker:
        return ticker
    return f"{ticker}{suffix}" if suffix else ticker


def build_dataframe(frames: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(frames, ignore_index=True).fillna("").drop_duplicates().set_index("")


def drop_header_rows_for_display(df: pd.DataFrame) -> pd.DataFrame:
    header_mask = df.apply(
        lambda row: all(str(row[col]).strip() == str(col).strip() for col in df.columns),
        axis=1,
    )
    return df.loc[~header_mask]


def write_output(df: pd.DataFrame, output: str | None, fmt: str) -> None:
    if fmt == "csv":
        if output:
            df.to_csv(output)
        else:
            df.to_csv(sys.stdout)
        return

    display_df = drop_header_rows_for_display(df)
    text = display_df.to_string()
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch FT Markets performance tables for tickers.",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        help="Inline tickers (space or comma separated).",
    )
    parser.add_argument(
        "--file",
        help="Path to a text/CSV file with tickers (use - for stdin).",
    )
    parser.add_argument(
        "--suffix",
        default=":PCQ:USD",
        help="Suffix to append to tickers missing an exchange.",
    )
    parser.add_argument(
        "--no-suffix",
        action="store_true",
        help="Do not append a suffix to tickers.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Request timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of retries per URL.",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=1.5,
        help="Backoff multiplier (seconds) between retries.",
    )
    parser.add_argument(
        "--output",
        help="Write CSV output to this path (stdout if omitted).",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "table"],
        default="csv",
        help="Output format (default: csv).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stop on the first ticker failure.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-ticker progress output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tickers = parse_tickers_from_args(args.tickers)
    if args.file:
        tickers.extend(parse_tickers_from_file(args.file))
    tickers = dedupe(tickers)
    if not tickers:
        raise SystemExit("Provide --tickers or --file with at least one ticker.")

    suffix = "" if args.no_suffix else args.suffix
    symbols = [normalize_symbol(ticker, suffix) for ticker in tickers]
    symbols = [symbol for symbol in symbols if symbol]

    settings = FetchSettings(timeout=args.timeout, retries=args.retries, backoff=args.backoff)
    frames: list[pd.DataFrame] = []

    for index, symbol in enumerate(symbols):
        if not args.quiet:
            print(symbol, file=sys.stderr)
        try:
            frames.append(frame_for_symbol(symbol, settings))
        except Exception as exc:
            if args.strict:
                raise
            print(f"warning: {symbol}: {exc}", file=sys.stderr)
        if args.sleep and index < len(symbols) - 1:
            time.sleep(args.sleep)

    if not frames:
        raise SystemExit("No data frames produced.")

    df = build_dataframe(frames)
    write_output(df, args.output, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
