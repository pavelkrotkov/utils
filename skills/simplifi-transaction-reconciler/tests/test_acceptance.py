"""End-to-end acceptance coverage for source adapters through HTML reports."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from simplifi_runtime.cli import build_parser
from simplifi_runtime.sources import api_source

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class FixtureApiClient:
    """Small read-only API double backed by the sanitized fixture JSON."""

    def __init__(self, payload: dict):
        self.payload = payload

    def accounts(self) -> list[dict]:
        return self.payload["accounts"]

    def categories(self) -> list[dict]:
        return self.payload["categories"]

    def transactions(
        self, date_on_after: str | None = None, modified_after: str | None = None
    ) -> list[dict]:
        del date_on_after, modified_after
        return self.payload["transactions"]


def _current_rows(db: Path, source: str) -> list[dict]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transaction_version "
                "WHERE source = ? AND is_current = 1 ORDER BY posted_on, transaction_id",
                (source,),
            )
        ]


def _run_ingest(arguments: list[str]) -> None:
    args = build_parser().parse_args(arguments)
    assert args.func(args) == 0


def _run_analyze(db: Path, out: Path) -> str:
    args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(out),
            "--today",
            "2026-06-15",
        ]
    )
    assert args.func(args) == 0
    return out.read_text(encoding="utf-8")


def test_csv_fixture_reaches_store_and_report_without_false_clean(tmp_path: Path):
    db = tmp_path / "csv.sqlite"
    report = tmp_path / "csv.html"

    _run_ingest(
        [
            "ingest",
            "--source",
            "csv",
            str(FIXTURE_DIR / "acceptance.csv"),
            "--db",
            str(db),
        ]
    )
    rows = _current_rows(db, "csv")
    html = _run_analyze(db, report)

    assert rows
    assert sum(row["review_eligible"] for row in rows) > 0
    excluded = next(row for row in rows if row["payee_display"] == "Ignored Purchase")
    assert excluded["review_eligible"] == 0
    assert "excluded_from_reports" in excluded["eligibility_reason_codes"]
    assert "CSV has no settlement or projection metadata" in html
    assert "Transactions</div><div class=v>7" in html


def test_api_fixture_reaches_report_with_review_uncategorized_and_recurring_findings(
    tmp_path: Path, monkeypatch
):
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        api_source,
        "client_from_env_or_age",
        lambda verbose=False: FixtureApiClient(payload),
    )
    db = tmp_path / "api.sqlite"
    report = tmp_path / "api.html"

    _run_ingest(["ingest", "--source", "api", "--full-rescan", "--db", str(db)])
    rows = _current_rows(db, "api")
    html = _run_analyze(db, report)

    assert rows
    assert sum(row["review_eligible"] for row in rows) > 0
    assert "subscription_creep" in html
    assert "MYSTERY PURCHASE" in html
    assert "LIMITATION: The API bulk transaction response did not expose" in html
    assert "Excluded from stats</div><div class=v>3" in html

    excluded = next(row for row in rows if row["transaction_id"] == "api-excluded")
    assert excluded["review_eligible"] == 0
    assert "excluded_from_reports" in excluded["eligibility_reason_codes"]

    transfer = next(row for row in rows if row["transaction_id"] == "api-transfer")
    assert transfer["kind"] == "transfer"
    assert transfer["poisons_statistics"] == 1
    assert "category matches an account name" in transfer["semantics_reasons"]

    unknown = next(row for row in rows if row["transaction_id"] == "api-unknown-exclusion")
    assert unknown["review_eligible"] == 1
    assert unknown["exclusion_flag"] == 2
    assert "report_exclusion_unknown" in unknown["eligibility_reason_codes"]
