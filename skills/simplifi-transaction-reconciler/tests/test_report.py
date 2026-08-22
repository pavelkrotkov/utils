from simplifi_runtime.money import Money
from simplifi_runtime.prioritize import Prioritized, Signal
from simplifi_runtime.report import render
from simplifi_runtime.subscriptions import RecurringFinding, SeriesRef


def test_csv_report_surfaces_capability_limit_and_row_provenance():
    row = {
        "transaction_id": "txn-17",
        "id": 17,
        "run_id": 4,
        "source_hash": "hash-17",
        "algorithm_version": "0.1.0",
        "ruleset_version": "0.1.0",
        "is_uncategorized": 1,
        "poisons_statistics": 0,
        "posted_on": "2026-08-01",
        "payee_display": "Example",
        "amount_minor_units": -100,
    }

    html = render(
        run_id=4,
        source="csv",
        analysis_date="2026-08-15",
        rows=[row],
        prioritized=[],
        staleness=[],
        proposals=[(row, None)],
        memory_stats={},
        limitations=["all settled-only analyses are unavailable"],
    )

    assert "all settled-only analyses are unavailable" in html
    assert "transaction_id=txn-17" in html
    assert "version_id=17" in html
    assert "run_id=4" in html
    assert "source_hash=hash-17" in html
    assert "analysis through 2026-08-15" in html


def test_report_renders_all_prioritized_rows_and_findings():
    def row(index):
        return {
            "transaction_id": f"txn-{index}",
            "id": index,
            "posted_on": "2026-08-01",
            "payee_display": f"Payee {index}",
            "account_name": "Checking",
            "amount_minor_units": -100,
        }

    prioritized = [Prioritized(row(index), [Signal("review", 1.0, {})]) for index in range(61)]
    findings = [
        RecurringFinding(
            kind="hike",
            series=(
                SeriesRef(
                    merchant=f"Merchant {index}",
                    account="",
                    transaction_ids=(f"tx-{index}",),
                    monthly=Money(100, "USD"),
                    interval_days=30.0,
                    last_charge="2026-08-01",
                ),
            ),
            detail="detail",
            annual_impact=Money(1200, "USD"),
        )
        for index in range(41)
    ]

    html = render(
        run_id=4,
        source="api",
        rows=[],
        prioritized=prioritized,
        staleness=[],
        proposals=[],
        memory_stats={},
        subscription_findings=findings,
    )

    assert "Payee 60" in html
    assert "Merchant 40" in html


def test_report_counts_unknown_exclusion_as_statistical_quarantine():
    row = {
        "transaction_id": "txn-1",
        "is_uncategorized": 0,
        "poisons_statistics": 0,
        "exclusion_flag": 2,
        "posted_on": "2026-08-01",
        "payee_display": "Example",
        "amount_minor_units": -100,
    }

    html = render(
        run_id=1,
        source="api",
        rows=[row],
        prioritized=[],
        staleness=[],
        proposals=[],
        memory_stats={},
    )

    assert "Excluded from stats</div><div class=v>1" in html
