from datetime import date

from simplifi_runtime.subscriptions import _is_recurring, _series, analyse


def _charges(payee, amounts, *, month0=1, state="CLEARED"):
    return [
        {
            "payee_canonical": payee,
            "kind": "spend",
            "txn_state": state,
            "amount_minor_units": -int(amount * 100),
            "posted_on": f"2026-{month0 + index:02d}-01",
        }
        for index, amount in enumerate(amounts)
    ]


def test_variable_bills_are_not_subscriptions_but_fixed_charges_are():
    utility = _charges("firstenergy", [167.70, 224.15, 98.30, 310.02])
    fixed = _charges("netflix", [15.99, 15.99, 15.99, 15.99])
    series = _series(utility + fixed)

    assert not _is_recurring(series["firstenergy"])
    assert _is_recurring(series["netflix"])


def test_successor_that_started_first_is_not_reported_as_rename():
    old = _charges("hbomax", [14.20] * 4)
    earlier = _charges("aliexpress", [15.00] * 4)

    findings = analyse(old + earlier, today=date(2026, 12, 1))

    assert "renamed" not in {finding.kind for finding in findings}


def test_projected_rows_do_not_sustain_a_subscription_series():
    rows = _charges("subscription", [31.98] * 3, state="PENDING")
    for row in rows:
        row["scheduled_model_id"] = "scheduled-1"
    series = _series(rows)["subscription"]

    assert series.charges == []
    assert len(series.projected) == 3
