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


def _proposals_document(packet: dict, transaction_id: str, **overrides) -> dict:
    document = {
        "document_type": "simplifi.transaction.proposals",
        "schema_version": "1",
        "packet": {
            "analysis_date": packet["run"]["analysis_date"],
            "dataset_hash": packet["source"]["dataset_hash"],
            "packet_type": packet["packet_type"],
            "run_id": packet["run"]["run_id"],
            "schema_version": packet["schema_version"],
        },
        "reviewer": {"kind": "agent", "id": "acceptance-agent"},
        "proposals": [
            {
                "proposal_id": "proposal-1",
                "transaction_id": transaction_id,
                "decision": "accept",
                "action": "record_category_proposal",
                "category": "Subscriptions",
                "rationale": "Recurring cleared charge matches the established subscription series.",
                "policy_references": ["ADR-004"],
            }
        ],
    }
    document.update(overrides)
    return document


def _run_decide(db: Path, packet_path: Path, proposals: Path, out: Path) -> int:
    args = build_parser().parse_args(
        [
            "decide",
            "--db",
            str(db),
            "--packet",
            str(packet_path),
            "--proposals",
            str(proposals),
            "--out",
            str(out),
        ]
    )
    return args.func(args)


def test_validated_proposals_become_records_without_touching_transactions(tmp_path: Path):
    db = tmp_path / "decide.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    subscription = next(
        transaction
        for transaction in packet["transactions"]
        if transaction["category"] == "Subscriptions"
    )
    before = _current_rows(db, "csv")

    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, subscription["transaction_id"])),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"

    assert _run_decide(db, packet_path, proposals, out) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["document_type"] == "simplifi.transaction.decisions"
    assert written["summary"] == {
        "decision_count": 1,
        "appended_count": 1,
        "already_recorded_count": 0,
    }
    record = written["records"][0]
    assert record["run_id"] == packet["run"]["run_id"]
    assert record["transaction_id"] == subscription["transaction_id"]
    assert record["validator_version"]
    assert record["proposal_hash"]

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        stored = [dict(row) for row in conn.execute("SELECT * FROM decision_record")]
    assert [item["decision_id"] for item in stored] == [record["decision_id"]]
    assert _current_rows(db, "csv") == before, "decisions must not alter transaction state"

    # Recording the same judgment again is a no-op, not a duplicate record.
    assert _run_decide(db, packet_path, proposals, out) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["appended_count"] == 0


def test_decide_rejects_stale_unknown_and_mutating_proposals(tmp_path: Path, capsys):
    db = tmp_path / "reject.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    transaction_id = packet["transaction_ids"][0]
    proposals = tmp_path / "proposals.json"
    out = tmp_path / "decisions.json"

    mutating = _proposals_document(packet, transaction_id)
    mutating["proposals"][0]["action"] = "apply_category"
    proposals.write_text(json.dumps(mutating), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "unsupported_action" in capsys.readouterr().err
    assert not out.exists()

    unknown = _proposals_document(packet, "not-a-transaction")
    proposals.write_text(json.dumps(unknown), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "unknown_transaction_id" in capsys.readouterr().err

    # A later successful run supersedes the packet the review was based on.
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    proposals.write_text(json.dumps(_proposals_document(packet, transaction_id)), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "stale_run_reference" in capsys.readouterr().err

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0
    assert not out.exists()


def test_decide_refuses_to_overwrite_its_inputs(tmp_path: Path):
    db = tmp_path / "collision.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    proposals = tmp_path / "proposals.json"
    proposals.write_text("{}", encoding="utf-8")
    packet_bytes = packet_path.read_bytes()

    for collision in (packet_path, proposals, db):
        assert _run_decide(db, packet_path, proposals, collision) == 2

    assert packet_path.read_bytes() == packet_bytes
    assert db.read_bytes().startswith(b"SQLite format 3\x00")


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
    packet = json.loads((report.parent / "review-packet.json").read_text(encoding="utf-8"))

    assert rows
    assert packet["schema_version"] == "1"
    assert packet["source"]["kind"] == "csv"
    assert packet["summary"]["eligible_transaction_count"] > 0
    assert packet["transaction_ids"]
    assert packet["examples"]
    assert all(example["id"].startswith("judgment-") for example in packet["examples"])
    assert sum(row["review_eligible"] for row in rows) > 0
    excluded = next(
        (row for row in rows if row["payee_display"] == "Ignored Purchase"),
        None,
    )
    assert excluded is not None, "Expected row with 'Ignored Purchase' payee not found"
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
    packet = json.loads((report.parent / "review-packet.json").read_text(encoding="utf-8"))

    assert rows
    assert packet["schema_version"] == "1"
    assert packet["source"]["kind"] == "api"
    assert packet["findings"]
    assert packet["examples"]
    assert any(
        example["title"] == "Projected versus real subscription" for example in packet["examples"]
    )
    assert all("payee_raw" not in transaction for transaction in packet["transactions"])
    assert sum(row["review_eligible"] for row in rows) > 0
    assert "subscription_creep" in html
    assert "MYSTERY PURCHASE" in html
    assert "LIMITATION: The API bulk transaction response did not expose" in html
    assert "Excluded from stats</div><div class=v>3" in html

    excluded = next(
        (row for row in rows if row["transaction_id"] == "api-excluded"),
        None,
    )
    assert excluded is not None, "Expected row with 'api-excluded' transaction_id not found"
    assert excluded["review_eligible"] == 0
    assert "excluded_from_reports" in excluded["eligibility_reason_codes"]

    transfer = next(
        (row for row in rows if row["transaction_id"] == "api-transfer"),
        None,
    )
    assert transfer is not None, "Expected row with 'api-transfer' transaction_id not found"
    assert transfer["kind"] == "transfer"
    assert transfer["poisons_statistics"] == 1
    assert "category matches an account name" in transfer["semantics_reasons"]

    unknown = next(
        (row for row in rows if row["transaction_id"] == "api-unknown-exclusion"),
        None,
    )
    assert unknown is not None, "Expected row with 'api-unknown-exclusion' transaction_id not found"
    assert unknown["review_eligible"] == 1
    assert unknown["exclusion_flag"] == 2
    assert "report_exclusion_unknown" in unknown["eligibility_reason_codes"]


def test_analyze_rejects_report_and_packet_path_collision(tmp_path: Path):
    db = tmp_path / "collision.sqlite"
    output = tmp_path / "review.html"
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

    args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(output),
            "--packet-out",
            str(output),
            "--today",
            "2026-06-15",
        ]
    )

    assert args.func(args) == 2
    assert not output.exists()

    db_collision_output = tmp_path / "db-collision.html"
    db_collision_args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(db_collision_output),
            "--packet-out",
            str(db),
            "--today",
            "2026-06-15",
        ]
    )

    assert db_collision_args.func(db_collision_args) == 2
    assert db.read_bytes().startswith(b"SQLite format 3\x00")
