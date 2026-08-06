from simplifi_runtime.prioritize import analyse


def test_pending_rows_do_not_create_review_signals():
    rows = [
        {
            "transaction_id": f"pending-{index}",
            "posted_on": "2026-08-01",
            "payee_canonical": "same_merchant",
            "payee_display": "Same Merchant",
            "account_name": "Checking",
            "amount_minor_units": -5000,
            "category": "Shopping",
            "kind": "spend",
            "poisons_statistics": 0,
            "txn_state": "PENDING",
        }
        for index in range(2)
    ]

    assert analyse(rows) == []
