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
import calendar
import csv
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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

ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
AS_OF_DATE_RE = re.compile(r"\bas of ([A-Za-z]{3} \d{1,2} \d{4})")
PERIOD_RE = re.compile(r"(\d+)\s*(month|year)s?", re.IGNORECASE)


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
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < settings.retries:
                time.sleep(settings.backoff * attempt)
    raise RuntimeError(f"Failed to fetch {url}") from last_exc


def normalize_currency(code: str) -> str:
    code = code.strip().upper()
    if code == "GBX":
        return "GBP"
    return code


def parse_target_currency(symbol: str) -> str:
    parts = [part.strip() for part in symbol.split(":") if part.strip()]
    if not parts:
        return ""
    candidate = parts[-1].upper()
    if len(candidate) == 3 and candidate.isalpha():
        return candidate
    return ""


def extract_first_value(df: pd.DataFrame, column: str) -> str:
    if column not in df.columns:
        return ""
    for value in df[column].tolist():
        text = str(value).strip()
        if not text:
            continue
        if text.lower() == column.lower():
            continue
        return text
    return ""


def is_blank_cell(value: object) -> bool:
    if value is None:
        return True
    if pd.isna(value):
        return True
    text = str(value).strip()
    return not text or text.lower() == "nan"


def parse_as_of_date(soup: BeautifulSoup) -> date:
    text = " ".join(soup.stripped_strings)
    match = AS_OF_DATE_RE.search(text)
    if not match:
        return date.today()
    date_text = match.group(1)
    try:
        return datetime.strptime(date_text, "%b %d %Y").date()
    except ValueError:
        parts = date_text.split()
        if len(parts) == 3 and len(parts[1]) == 1:
            padded = f"{parts[0]} 0{parts[1]} {parts[2]}"
            try:
                return datetime.strptime(padded, "%b %d %Y").date()
            except ValueError:
                return date.today()
    return date.today()


def subtract_months(value: date, months: int) -> date:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def period_start_date(end_date: date, label: str) -> date | None:
    if not label:
        return None
    label = label.strip().lower()
    if label in {"ytd", "year to date"}:
        return date(end_date.year, 1, 1)
    match = PERIOD_RE.search(label)
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2).lower()
    months = count if unit.startswith("month") else count * 12
    return subtract_months(end_date, months)


def parse_percent(value: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or "%" not in text:
        return None
    text = text.replace("%", "").replace(",", "").replace("+", "")
    text = text.replace("−", "-")
    try:
        return float(text) / 100.0
    except ValueError:
        return None


def format_percent(value: float) -> str:
    return f"{value * 100:+.2f}%"


def fetch_ecb_rates(settings: FetchSettings) -> dict[date, dict[str, float]]:
    response = requests.get(ECB_RATES_URL, headers=get_random_headers(), timeout=settings.timeout)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    rates_by_date: dict[date, dict[str, float]] = {}
    for cube in root.findall(".//{*}Cube[@time]"):
        time_value = cube.attrib.get("time")
        if not time_value:
            continue
        try:
            parsed_date = datetime.strptime(time_value, "%Y-%m-%d").date()
        except ValueError:
            continue
        day_rates: dict[str, float] = {"EUR": 1.0}
        for child in cube.findall("{*}Cube"):
            currency = child.attrib.get("currency")
            rate_value = child.attrib.get("rate")
            if not currency or not rate_value:
                continue
            try:
                day_rates[currency.upper()] = float(rate_value)
            except ValueError:
                continue
        rates_by_date[parsed_date] = day_rates
    if not rates_by_date:
        raise RuntimeError("No FX rates found in ECB response")
    return rates_by_date


def get_rate_on_or_before(
    rates_by_date: dict[date, dict[str, float]],
    target_date: date,
    currency: str,
) -> float | None:
    currency = normalize_currency(currency)
    if not currency:
        return None
    min_date = min(rates_by_date)
    current = target_date
    while current >= min_date:
        day_rates = rates_by_date.get(current)
        if day_rates and currency in day_rates:
            return day_rates[currency]
        current -= timedelta(days=1)
    return None


def fx_rate_on(
    rates_by_date: dict[date, dict[str, float]],
    target_date: date,
    base_currency: str,
    target_currency: str,
) -> float | None:
    base_rate = get_rate_on_or_before(rates_by_date, target_date, base_currency)
    target_rate = get_rate_on_or_before(rates_by_date, target_date, target_currency)
    if base_rate is None or target_rate is None:
        return None
    return target_rate / base_rate


def fx_return_for_period(
    rates_by_date: dict[date, dict[str, float]],
    base_currency: str,
    target_currency: str,
    start_date: date,
    end_date: date,
) -> float | None:
    start_rate = fx_rate_on(rates_by_date, start_date, base_currency, target_currency)
    end_rate = fx_rate_on(rates_by_date, end_date, base_currency, target_currency)
    if start_rate is None or end_rate is None:
        return None
    return end_rate / start_rate - 1.0


def adjust_performance_table(
    performance: pd.DataFrame,
    base_currency: str,
    target_currency: str,
    as_of: date,
    rates_by_date: dict[date, dict[str, float]] | None,
) -> tuple[pd.DataFrame, str, bool]:
    base_currency = normalize_currency(base_currency)
    target_currency = normalize_currency(target_currency)
    return_currency = base_currency or target_currency
    if not base_currency or not target_currency or base_currency == target_currency:
        return performance, return_currency, False
    if not rates_by_date:
        return performance, return_currency, False

    fx_returns: dict[str, float] = {}
    for column in performance.columns:
        start = period_start_date(as_of, column)
        if start is None:
            continue
        fx_return = fx_return_for_period(
            rates_by_date, base_currency, target_currency, start, as_of
        )
        if fx_return is None:
            continue
        fx_returns[column] = fx_return

    if not fx_returns:
        return performance, return_currency, False

    def adjust_value(value: str, fx_return: float | None) -> str:
        if fx_return is None:
            return value
        parsed = parse_percent(value)
        if parsed is None:
            return value
        return format_percent((1 + parsed) * (1 + fx_return) - 1.0)

    adjusted = performance.copy()
    for column, fx_return in fx_returns.items():
        adjusted[column] = adjusted[column].apply(
            lambda value, fx_return=fx_return: adjust_value(value, fx_return)
        )
    return adjusted, target_currency, True


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


def frame_for_symbol(
    symbol: str,
    settings: FetchSettings,
    fx_rates: dict[date, dict[str, float]] | None,
    quiet: bool,
) -> pd.DataFrame:
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

    summary_details = frame_from_html(summary_tables[0], True)
    base_currency = extract_first_value(summary_details, "Price currency")
    as_of_date = parse_as_of_date(summary_soup)
    target_currency = parse_target_currency(symbol)

    performance_frame = frame_from_html(performance_table)[:3]
    performance_frame, return_currency, conversion_applied = adjust_performance_table(
        performance_frame,
        base_currency,
        target_currency,
        as_of_date,
        fx_rates,
    )
    if (
        base_currency
        and target_currency
        and normalize_currency(base_currency) != normalize_currency(target_currency)
        and not conversion_applied
        and not quiet
    ):
        print(
            f"warning: {symbol}: FX conversion skipped (base={base_currency}, target={target_currency})",
            file=sys.stderr,
        )

    summary_details["Return currency"] = return_currency
    summary_details = summary_details.reindex(
        columns=[
            "Morningstar category",
            "Launch date",
            "Price currency",
            "Return currency",
            "Symbol",
        ]
    )
    summary_financials = frame_from_html(summary_tables[1], True).reindex(
        columns=["Total net assets", "Net expense ratio"]
    )
    net_expense_ratio = extract_first_value(summary_financials, "Net expense ratio")
    if not net_expense_ratio:
        net_expense_ratio = extract_first_value(summary_details, "Ongoing charge")
    if net_expense_ratio and "Net expense ratio" in summary_financials.columns:
        summary_financials["Net expense ratio"] = summary_financials["Net expense ratio"].apply(
            lambda value: net_expense_ratio if is_blank_cell(value) else value
        )

    return pd.concat(
        [
            performance_frame,
            summary_details,
            summary_financials,
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
    content = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")

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
    reference_date = date.today()
    period_columns = [
        col for col in df.columns if period_start_date(reference_date, str(col)) is not None
    ]
    columns_to_check = period_columns or list(df.columns)
    header_mask = df.apply(
        lambda row: all(str(row[col]).strip() == str(col).strip() for col in columns_to_check),
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
    fx_rates: dict[date, dict[str, float]] | None = None
    try:
        fx_rates = fetch_ecb_rates(settings)
    except requests.RequestException as exc:
        if not args.quiet:
            print(f"warning: FX rates unavailable ({exc})", file=sys.stderr)
    frames: list[pd.DataFrame] = []

    for index, symbol in enumerate(symbols):
        if not args.quiet:
            print(symbol, file=sys.stderr)
        try:
            frames.append(frame_for_symbol(symbol, settings, fx_rates, args.quiet))
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
