from __future__ import annotations

import json
from pathlib import Path

import pytest
from simplifi_runtime.money import Money
from simplifi_runtime.prioritize import Prioritized, Signal
from simplifi_runtime.review_packet import (
    PacketValidationError,
    build_packet,
    validate_packet,
    write_packet,
)
from simplifi_runtime.subscriptions import RecurringFinding, SeriesRef

FIXTURE = Path(__file__).parent / "fixtures" / "review_packet.json"


def _row(transaction_id: str, *, eligible: int = 1) -> dict:
    return {
        "transaction_id": transaction_id,
        "id": 7,
        "run_id": 42,
        "source_hash": f"hash-{transaction_id}",
        "algorithm_version": "0.1.0",
        "ruleset_version": "0.2.0",
        "posted_on": "2026-08-01",
        "transacted_on": "2026-07-31",
        "account_name": "Checking",
        "account_id": "sensitive-account-id",
        "amount_minor_units": -1250,
        "currency": "USD",
        "currency_exponent": 2,
        "payee_raw": "RAW*SECRET DESCRIPTOR",
        "payee_normalized": "Fixture Market",
        "payee_canonical": "fixture market",
        "payee_display": "RAW*SECRET DESCRIPTOR",
        "category": "Shopping",
        "inferred_category": None,
        "is_uncategorized": 0,
        "exclusion_flag": 0 if eligible else 1,
        "recurring_flag": 0,
        "is_split": 0,
        "is_reviewed": 1,
        "kind": "spend",
        "poisons_statistics": 0,
        "semantics_reasons": "",
        "txn_state": "CLEARED",
        "match_state": "unknown",
        "review_eligible": eligible,
        "eligibility_reason_codes": "eligible" if eligible else "excluded_from_reports",
    }


def test_fixture_is_a_valid_documented_contract():
    packet = json.loads(FIXTURE.read_text(encoding="utf-8"))

    validate_packet(packet)


def test_packet_is_deterministic_and_omits_raw_or_secret_fields(tmp_path: Path):
    row = _row("txn-1")
    prioritized = [
        Prioritized(row, [Signal("amount_outlier", 2.5, {"ratio": 4.0, "account": "Checking"})])
    ]

    first = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[row],
        prioritized=prioritized,
        proposals=[],
    )
    second = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[row],
        prioritized=prioritized,
        proposals=[],
    )
    first_path = tmp_path / "one.json"
    second_path = tmp_path / "two.json"
    write_packet(first, first_path)
    write_packet(second, second_path)

    assert first_path.read_bytes() == second_path.read_bytes()
    encoded = first_path.read_text(encoding="utf-8")
    assert "RAW*SECRET DESCRIPTOR" not in encoded
    assert "sensitive-account-id" not in encoded
    assert '"payee_raw"' not in encoded
    assert '"account_id"' not in encoded
    assert first["findings"][0]["reason_codes"] == ["amount_outlier"]


def test_packet_preserves_zero_currency_exponent_and_projection_state():
    row = _row("jpy-1")
    row.update(
        {
            "amount_minor_units": -1500,
            "currency": "JPY",
            "currency_exponent": 0,
            "txn_state": "PENDING",
            "scheduled_model_id": "internal-schedule-id",
        }
    )

    packet = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[row],
        prioritized=[
            Prioritized(
                row,
                [
                    Signal(
                        "amount_outlier",
                        2.0,
                        {
                            "amount": 15.0,
                            "amount_minor_units": 1500,
                            "median": 1.0,
                            "median_minor_units": 100,
                        },
                    )
                ],
            )
        ],
        proposals=[],
    )

    transaction = packet["transactions"][0]
    assert transaction["amount"]["currency_exponent"] == 0
    assert transaction["flags"]["projected"] is True
    assert "internal-schedule-id" not in json.dumps(packet)
    facts = packet["findings"][0]["evidence"][0]["facts"]
    assert facts["amount"] == {
        "currency": "JPY",
        "currency_exponent": 0,
        "minor_units": 1500,
    }
    assert facts["median"]["minor_units"] == 100


def test_ineligible_rows_are_diagnostic_only():
    packet = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[_row("eligible"), _row("excluded", eligible=0)],
        prioritized=[],
        proposals=[],
    )

    assert packet["transaction_ids"] == ["eligible"]
    assert packet["summary"]["transaction_count"] == 2
    assert packet["summary"]["eligible_transaction_count"] == 1
    assert packet["excluded_transactions"] == [
        {
            "transaction_id": "excluded",
            "reason_codes": ["accounting_kind:spend", "excluded_from_reports"],
        }
    ]


def test_packet_uses_safe_merchant_display_and_account_fallback():
    row = _row("fallback-1")
    row["account_name"] = row["account_id"]

    packet = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[row],
        prioritized=[],
        proposals=[],
    )

    transaction = packet["transactions"][0]
    assert transaction["merchant"]["display"] == "Fixture Market"
    assert transaction["account_name"] == "unknown account"
    assert "RAW*SECRET DESCRIPTOR" not in json.dumps(packet)
    assert "sensitive-account-id" not in json.dumps(packet)


def test_recurring_finding_does_not_expose_internal_series_key():
    packet = build_packet(
        run_id=42,
        source="api",
        analysis_date="2026-08-15",
        rows=[],
        prioritized=[],
        proposals=[],
        subscription_findings=[
            RecurringFinding(
                kind="hike",
                series=(
                    SeriesRef(
                        merchant="fixture market",
                        account="Checking",
                        transaction_ids=("tx-1", "tx-2"),
                        monthly=Money(1200, "USD"),
                        interval_days=30.0,
                        last_charge="2026-08-01",
                    ),
                ),
                detail="price increased",
                annual_impact=Money(2400, "USD"),
                amounts={"previous": Money(1000, "USD"), "current": Money(1200, "USD")},
                facts={"ratio": 1.2},
            )
        ],
    )

    encoded = json.dumps(packet, sort_keys=True)
    assert "sensitive-account-id" not in encoded
    assert "series_key" not in encoded
    assert packet["findings"][0]["transaction_ids"] == ["tx-1", "tx-2"]
    evidence = packet["findings"][0]["evidence"]
    assert evidence["annual_impact"] == {
        "minor_units": 2400,
        "currency": "USD",
        "currency_exponent": 2,
    }
    assert evidence["amounts"]["previous"]["minor_units"] == 1000
    assert evidence["series"][0]["account_name"] == "Checking"


def test_validation_rejects_credentials():
    packet = json.loads(FIXTURE.read_text(encoding="utf-8"))
    packet["credentials"] = {"access_token": "do-not-ship"}

    with pytest.raises(PacketValidationError, match="forbidden"):
        validate_packet(packet)


def test_validation_requires_projection_flag_and_safe_examples():
    packet = json.loads(FIXTURE.read_text(encoding="utf-8"))
    packet["transactions"][0]["flags"].pop("projected")

    with pytest.raises(PacketValidationError, match="projected"):
        validate_packet(packet)

    packet = json.loads(FIXTURE.read_text(encoding="utf-8"))
    packet["examples"] = [{"account_id": "should-not-ship", "title": "unsafe"}]

    with pytest.raises(PacketValidationError, match=r"unsupported|forbidden"):
        validate_packet(packet)
