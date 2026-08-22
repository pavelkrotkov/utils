"""One structured result per recurring finding kind, with fixtures for each.

Recurring analysis used to hand its consumers a kind, a display string and a
major-unit float, and put the rest of the meaning in an English sentence. The
packet then guessed the finding's currency from a member transaction and the
report formatted the float itself. These fixtures pin the structure instead:
each kind carries its series references, its member transaction IDs, and money
that knows its own currency.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from simplifi_runtime import judgment_examples, report, review_packet
from simplifi_runtime.money import Money
from simplifi_runtime.subscriptions import FINDING_KINDS, RecurringFinding, analyse

TODAY = date(2027, 2, 1)


def _rows(
    payee,
    amounts,
    *,
    start=(2026, 1),
    state="CLEARED",
    step=1,
    account_name=None,
    account_id=None,
    prefix=None,
    currency="USD",
    scheduled=False,
):
    """Charges on a monthly-ish cadence, one per entry in `amounts`."""
    rows = []
    year, month = start
    for index, amount in enumerate(amounts):
        offset = month + index * step
        rows.append(
            {
                "transaction_id": f"{prefix or payee}-{index}",
                "payee_canonical": payee,
                "kind": "spend",
                "txn_state": state,
                "amount_minor_units": -amount,
                "currency": currency,
                "posted_on": f"{year + (offset - 1) // 12:04d}-{(offset - 1) % 12 + 1:02d}-01",
                **({"account_name": account_name} if account_name else {}),
                **({"account_id": account_id} if account_id else {}),
                **({"scheduled_model_id": "scheduled-1"} if scheduled else {}),
            }
        )
    return rows


# --- the six fixtures -------------------------------------------------------


def ghost_rows():
    """Charged quarterly, stopped, and Simplifi still projects it forward."""
    return _rows("quarterly_service", [10000] * 3, start=(2026, 1), step=3) + _rows(
        "quarterly_service",
        [10000] * 3,
        start=(2026, 10),
        step=3,
        state="PENDING",
        scheduled=True,
        prefix="quarterly_service_projected",
    )


def projection_only_rows():
    """A schedule the user set up by hand: projections and nothing else."""
    return _rows(
        "never_charged",
        [10000] * 3,
        start=(2027, 3),
        state="PENDING",
        scheduled=True,
    )


def lapsed_rows():
    """Regular for four months, then silence, with nothing projected."""
    return _rows("gonequiet", [1599] * 4, start=(2026, 1))


def zombie_rows():
    """The schedule ran out and the billing carried on."""
    return _rows(
        "outlived",
        [1599] * 3,
        start=(2026, 9),
        prefix="outlived_projected",
        state="PENDING",
        scheduled=True,
    ) + _rows("outlived", [1599] * 6, start=(2026, 9))


def hike_rows():
    """Four charges at one price, two at a materially higher one."""
    return _rows("streamline_video", [1000, 1000, 1000, 1000, 2000, 2000], start=(2026, 9))


def multi_account_twin_rows():
    """The same provider billing two accounts concurrently."""
    return _rows(
        "shared_provider",
        [1000] * 4,
        start=(2026, 11),
        account_name="Checking",
        account_id="account-id-one",
        prefix="checking-shared-provider",
    ) + _rows(
        "shared_provider",
        [1000] * 4,
        start=(2026, 11),
        account_name="Savings",
        account_id="account-id-two",
        prefix="savings-shared-provider",
    )


FIXTURES = {
    "ghost": ghost_rows,
    "lapsed": lapsed_rows,
    "zombie": zombie_rows,
    "hike": hike_rows,
    "twin": multi_account_twin_rows,
}


def _finding(kind: str) -> RecurringFinding:
    findings = analyse(FIXTURES[kind](), today=TODAY)
    return next(finding for finding in findings if finding.kind == kind)


@pytest.mark.parametrize("kind", sorted(FIXTURES))
def test_each_kind_carries_a_complete_structured_result(kind):
    finding = _finding(kind)

    assert finding.kind in FINDING_KINDS
    assert finding.series, "a finding names the series it is about"
    assert finding.transaction_ids, "a finding names its contributing transactions"
    assert all(ref.merchant for ref in finding.series)
    assert isinstance(finding.annual_impact, Money)
    assert all(isinstance(money, Money) for money in finding.amounts.values())
    # Every member ID belongs to one of the named series, so a consumer can
    # attribute any transaction back without re-running the analysis.
    from_series = {txid for ref in finding.series for txid in ref.transaction_ids}
    assert set(finding.transaction_ids) == from_series


def test_a_projection_only_series_produces_no_finding():
    """A hand-made bill reminder that never charged is expected, not a ghost.

    It is also the shape that once crashed the analysis: `last_charge` takes
    `max()` of an empty list when a series is pure projection.
    """
    assert analyse(projection_only_rows(), today=TODAY) == []


def test_a_hike_states_both_price_regimes_as_money():
    hike = _finding("hike")

    assert hike.amounts["previous"] == Money(1000, "USD")
    assert hike.amounts["current"] == Money(2000, "USD")
    assert hike.facts["ratio"] == pytest.approx(2.0)


def test_a_lapsed_finding_reports_a_negative_annual_impact():
    """The money stopped, so the yearly effect is a saving, not a cost."""
    lapsed = _finding("lapsed")

    assert lapsed.annual_impact.minor_units < 0
    assert lapsed.amounts["monthly"].minor_units > 0
    assert lapsed.facts["silent_days"] > 0


def test_a_ghost_reports_the_projected_charge_not_a_real_one():
    ghost = _finding("ghost")

    assert ghost.amounts["projected_charge"] == Money(10000, "USD")
    assert ghost.facts["projected_count"] >= 1


def test_a_zombie_counts_the_charges_that_outlived_the_schedule():
    zombie = _finding("zombie")

    assert zombie.facts["charges_after_schedule"] >= 1
    assert zombie.annual_impact.minor_units > 0


def test_a_multi_account_twin_names_both_series_separately():
    twin = _finding("twin")

    assert len(twin.series) == 2
    assert {ref.account for ref in twin.series} == {"Checking", "Savings"}
    assert twin.facts["shared_token"] == "shared"
    assert twin.facts["mixed_currency"] is False


def test_a_finding_in_a_non_default_currency_stays_in_that_currency():
    """A ¥ hike reported in dollars is a number a reader would act on."""
    rows = _rows(
        "streamline_video",
        [1000, 1000, 1000, 1000, 2000, 2000],
        start=(2026, 9),
        currency="JPY",
    )
    hike = next(finding for finding in analyse(rows, today=TODAY) if finding.kind == "hike")

    assert hike.annual_impact.currency == "JPY"
    assert hike.annual_impact.exponent == 0, "JPY has no minor units"
    assert hike.amounts["current"] == Money(2000, "JPY")


# --- what may never leave ---------------------------------------------------


@pytest.mark.parametrize("kind", sorted(FIXTURES))
def test_no_internal_key_or_provider_account_id_reaches_an_artifact(kind):
    """Packet, report and classifier context all read the same safe result."""
    rows = FIXTURES[kind]()
    findings = analyse(rows, today=TODAY)

    packet = review_packet.build_packet(
        run_id=1,
        source="csv",
        analysis_date=TODAY.isoformat(),
        rows=rows,
        prioritized=[],
        proposals=[],
        subscription_findings=findings,
    )
    rendered = report.render(
        run_id=1,
        source="csv",
        analysis_date=TODAY.isoformat(),
        rows=rows,
        prioritized=[],
        staleness=[],
        proposals=[],
        memory_stats={},
        subscription_findings=findings,
    )
    topic = judgment_examples.context_from_review([], findings, [])

    encoded = json.dumps(packet, sort_keys=True)
    for artifact in (encoded, rendered, str(topic)):
        assert "account-id-one" not in artifact
        assert "account-id-two" not in artifact
        assert "series_key" not in artifact
        # The internal key joins merchant and account correlation with `::`.
        assert "::" not in artifact


def test_the_packet_transcribes_the_result_rather_than_re_deriving_it():
    """One structure, two renderings — they cannot disagree about a figure."""
    rows = hike_rows()
    findings = analyse(rows, today=TODAY)
    packet = review_packet.build_packet(
        run_id=1,
        source="csv",
        analysis_date=TODAY.isoformat(),
        rows=rows,
        prioritized=[],
        proposals=[],
        subscription_findings=findings,
    )
    evidence = next(
        finding["evidence"]
        for finding in packet["findings"]
        if finding["scope"] == "merchant_series"
    )

    assert evidence["kind"] == findings[0].kind
    assert evidence["annual_impact"]["minor_units"] == findings[0].annual_impact.minor_units
    assert evidence["amounts"]["current"]["minor_units"] == 2000
    assert evidence["series"][0]["transaction_ids"] == sorted(findings[0].series[0].transaction_ids)
    review_packet.validate_packet(packet)
