from datetime import date

from simplifi_runtime.prioritize import activity_staleness, analyse


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


def test_activity_staleness_ignores_future_projections():
    rows = [
        {
            "account_name": "Checking",
            "posted_on": "2026-07-01",
            "txn_state": "CLEARED",
        },
        {
            "account_name": "Checking",
            "posted_on": "2026-12-01",
            "txn_state": "PENDING",
            "scheduled_model_id": "scheduled-1",
        },
    ]

    result = activity_staleness(rows, today=date(2026, 8, 1))

    assert result == [
        {
            "account": "Checking",
            "last_transaction": "2026-07-01",
            "days_stale": 31,
            "status": "stale",
        }
    ]


def test_subscription_creep_ignores_refunds_and_income():
    rows = []
    for index, posted_on in enumerate(("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01")):
        rows.append(
            {
                "transaction_id": f"charge-{index}",
                "posted_on": posted_on,
                "payee_canonical": "merchant",
                "payee_display": "Merchant",
                "account_name": "Checking",
                "amount_minor_units": -1000,
                "category": "Subscriptions",
                "kind": "spend",
                "poisons_statistics": 0,
            }
        )
    rows.append(
        {
            "transaction_id": "refund-1",
            "posted_on": "2026-05-01",
            "payee_canonical": "merchant",
            "payee_display": "Merchant",
            "account_name": "Checking",
            "amount_minor_units": 5000,
            "category": "Subscriptions",
            "kind": "refund",
            "poisons_statistics": 0,
        }
    )

    signals = [signal.name for item in analyse(rows) for signal in item.signals]

    assert "subscription_creep" not in signals


def test_short_history_does_not_suppress_a_real_amount_outlier():
    rows = []
    for index, amount in enumerate([-10.00] * 5 + [-1000.00]):
        rows.append(
            {
                "transaction_id": f"txn-{index}",
                "posted_on": f"2026-0{index + 1}-01",
                "payee_canonical": "merchant",
                "payee_display": "Merchant",
                "account_name": "Checking",
                "amount_minor_units": int(amount * 100),
                "category": "Shopping",
                "kind": "spend",
                "poisons_statistics": 0,
                "txn_state": "CLEARED",
            }
        )

    signals = [signal.name for item in analyse(rows) for signal in item.signals]

    assert "amount_outlier" in signals


def test_refunds_do_not_raise_the_spending_baseline():
    rows = []
    for index in range(5):
        rows.append(
            {
                "transaction_id": f"debit-{index}",
                "posted_on": f"2026-0{index + 1}-01",
                "payee_canonical": "merchant",
                "payee_display": "Merchant",
                "account_name": "Checking",
                "amount_minor_units": -1000,
                "category": "Shopping",
                "kind": "spend",
                "poisons_statistics": 0,
                "txn_state": "CLEARED",
            }
        )
    for index in range(3):
        rows.append(
            {
                "transaction_id": f"refund-{index}",
                "posted_on": f"2026-0{index + 6}-01",
                "payee_canonical": "merchant",
                "payee_display": "Merchant",
                "account_name": "Checking",
                "amount_minor_units": 90000,
                "category": "Shopping",
                "kind": "refund",
                "poisons_statistics": 0,
                "txn_state": "CLEARED",
            }
        )
    rows.append(
        {
            "transaction_id": "large-debit",
            "posted_on": "2026-09-01",
            "payee_canonical": "merchant",
            "payee_display": "Merchant",
            "account_name": "Checking",
            "amount_minor_units": -100000,
            "category": "Shopping",
            "kind": "spend",
            "poisons_statistics": 0,
            "txn_state": "CLEARED",
        }
    )

    signals = [signal.name for item in analyse(rows) for signal in item.signals]

    assert "amount_outlier" in signals


def test_subscription_creep_keeps_accounts_separate():
    rows = []
    for account, amounts in (
        ("Checking", [-10.00, -10.00, -10.00, -10.00, -12.00]),
        ("Savings", [-10.00, -10.00, -10.00, -10.00, -10.00]),
    ):
        for index, amount in enumerate(amounts):
            rows.append(
                {
                    "transaction_id": f"{account}-{index}",
                    "posted_on": f"2026-0{index + 1}-01",
                    "payee_canonical": "shared_provider",
                    "payee_display": "Shared Provider",
                    "account_name": account,
                    "amount_minor_units": int(amount * 100),
                    "category": "Subscriptions",
                    "kind": "spend",
                    "poisons_statistics": 0,
                    "txn_state": "CLEARED",
                }
            )

    signals = [signal for item in analyse(rows) for signal in item.signals]

    assert any(signal.name == "subscription_creep" for signal in signals)
