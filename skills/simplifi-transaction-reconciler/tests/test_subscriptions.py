from datetime import date

import pytest
from simplifi_runtime.subscriptions import _is_recurring, _series, analyse, summary


def _charges(payee, amounts, *, month0=1, state="CLEARED", account_name=None):
    return [
        {
            "payee_canonical": payee,
            "kind": "spend",
            "txn_state": state,
            "amount_minor_units": -int(amount * 100),
            "posted_on": f"2026-{month0 + index:02d}-01",
            **({"account_name": account_name} if account_name else {}),
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


def test_historical_projections_do_not_hide_a_lapsed_series():
    charges = _charges("subscription", [31.98] * 3, month0=1)
    historical_projection = _charges("subscription", [31.98], month0=4, state="PENDING")
    historical_projection[0]["scheduled_model_id"] = "scheduled-1"

    findings = analyse(charges + historical_projection, today=date(2026, 12, 1))

    assert "ghost" not in {finding.kind for finding in findings}
    assert "lapsed" in {finding.kind for finding in findings}


def test_lapsed_series_are_not_counted_as_live():
    old = _charges("old_subscription", [10.00] * 3, month0=1)
    active = _charges("active_subscription", [10.00] * 3, month0=9)

    result = summary(old + active, today=date(2026, 12, 1))

    assert result.startswith("1 live subscriptions")


def test_same_merchant_on_two_accounts_has_separate_cadence_series():
    first = _charges("shared_provider", [10.00] * 3, month0=9, account_name="Checking")
    second = _charges("shared_provider", [10.00] * 3, month0=9, account_name="Savings")

    series = _series(first + second)

    assert len(series) == 2
    assert all(_is_recurring(item) for item in series.values())
    assert summary(first + second, today=date(2026, 12, 1)).startswith("2 live subscriptions")


def test_ghost_annual_impact_uses_observed_quarterly_cadence():
    rows = [
        {
            "payee_canonical": "quarterly_service",
            "kind": "spend",
            "txn_state": "CLEARED",
            "amount_minor_units": -10000,
            "posted_on": posted_on,
        }
        for posted_on in ("2026-01-01", "2026-04-01", "2026-07-01")
    ]
    rows.extend(
        {
            "payee_canonical": "quarterly_service",
            "kind": "spend",
            "txn_state": "PENDING",
            "scheduled_model_id": "scheduled-1",
            "amount_minor_units": -10000,
            "posted_on": posted_on,
        }
        for posted_on in ("2026-10-01", "2027-01-01", "2027-04-01")
    )

    findings = analyse(rows, today=date(2027, 2, 1))
    ghost = next(finding for finding in findings if finding.kind == "ghost")

    assert ghost.annual_impact == pytest.approx(100 * 365.25 / 90, rel=0.02)


def test_repeated_merchant_token_does_not_create_self_twin():
    rows = _charges("netflix_netflix", [15.99] * 3, month0=9)

    findings = analyse(rows, today=date(2026, 12, 1))

    assert "twin" not in {finding.kind for finding in findings}


def test_price_hike_is_reported_even_when_recent_variation_is_high():
    rows = _charges("hike_service", [10.00, 10.00, 10.00, 10.00, 20.00, 20.00])

    findings = analyse(rows, today=date(2026, 7, 1))

    assert "hike" in {finding.kind for finding in findings}


def test_single_latest_charge_is_not_a_stable_price_hike():
    rows = _charges("hike_service", [10.00, 10.00, 10.00, 20.00])

    findings = analyse(rows, today=date(2026, 5, 1))

    assert "hike" not in {finding.kind for finding in findings}
