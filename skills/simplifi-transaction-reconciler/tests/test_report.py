from simplifi_runtime.report import render


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
