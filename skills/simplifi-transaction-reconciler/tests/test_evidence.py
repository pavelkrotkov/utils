"""The source seam: do the two adapters mean the same thing by the same field?

The bugs this file is about were not crashes. Each one produced a plausible
record that a downstream reader believed: a provider account ID rendered as an
account name, a raw bank descriptor published as a merchant name, an amount a
hundred times too small. So most of these tests compare two paths against each
other rather than against a constant — an assertion about one adapter alone
would have passed the entire time the two disagreed.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, cast

import pytest
from simplifi_runtime import egress, evidence, review_packet
from simplifi_runtime.money import Money
from simplifi_runtime.sources.api_source import SimplifiApiClient, SimplifiApiSource
from simplifi_runtime.sources.csv_source import SimplifiCsvSource

#: The same four transactions, told twice. The CSV carries Simplifi's renamed
#: payee and no IDs; the API carries the bank's descriptor, a stable ID, and
#: settlement state. Equivalent facts, not equivalent fields.
#:
#: The descriptors here are ones normalization reduces to the CSV's merchant —
#: a processor prefix, a foreign-charge prefix, an exact echo. That is the case
#: where the two sources really do state the same fact, and it is the case
#: these tests are about. Where a descriptor carries a store number and a
#: location the CSV never had, the sources state *different* facts and no
#: amount of normalization makes them one; see the divergence test below.
SHARED_TRANSACTIONS = [
    {
        "csv_payee": "Costco",
        "api_payee": "Costco",
        "category": "Groceries",
        "amount": "-84.20",
        "account": "Everyday Checking",
        "date": "Aug 1, 2026",
        "iso_date": "2026-08-01",
    },
    {
        "csv_payee": "Aurora Bakery",
        "api_payee": "SQ *Aurora Bakery",
        "category": "Restaurants",
        "amount": "-12.40",
        "account": "Everyday Checking",
        "date": "Aug 2, 2026",
        "iso_date": "2026-08-02",
    },
    {
        "csv_payee": "Mercadona Calella",
        "api_payee": "2.90 Euro Mercadona Calella",
        "category": "Groceries",
        "amount": "-3.15",
        "account": "Travel Card",
        "date": "Aug 3, 2026",
        "iso_date": "2026-08-03",
    },
    {
        "csv_payee": "Streamline Video",
        "api_payee": "Streamline Video",
        "category": "Subscriptions",
        "amount": "-10.00",
        "account": "Travel Card",
        "date": "Aug 4, 2026",
        "iso_date": "2026-08-04",
    },
]

CSV_COLUMNS = [
    "Date",
    "Account",
    "Reviewed",
    "Payee",
    "Category",
    "Attachments",
    "Exclusion",
    "Recurring",
    "Amount",
]


def write_csv(path: Path, transactions: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for txn in transactions:
            writer.writerow(
                [
                    txn["date"],
                    txn["account"],
                    "Yes",
                    txn["csv_payee"],
                    txn["category"],
                    "",
                    "No",
                    "No",
                    txn["amount"],
                ]
            )
    return path


def api_source_serving(transactions: list[dict], accounts: list[dict]) -> SimplifiApiSource:
    """An API source over canned responses, with no network and no auth."""
    client = object.__new__(SimplifiApiClient)
    client.accounts = cast(Any, lambda: accounts)
    client.categories = cast(
        Any,
        lambda: [
            {"id": txn["category"], "fullName": txn["category"]} for txn in SHARED_TRANSACTIONS
        ],
    )
    client.transactions = cast(
        Any,
        lambda *_args, **_kwargs: type(
            "Page", (), {"resources": transactions, "as_of": "2026-08-05T00:00:00Z"}
        )(),
    )
    source = object.__new__(SimplifiApiSource)
    source.client = client
    source.date_on_after = None
    source.modified_after = None
    source.as_of = None
    return source


def api_transaction(txn: dict[str, str], **overrides: Any) -> dict:
    record = {
        "id": f"api-{txn['iso_date']}",
        "postedOn": txn["iso_date"],
        "amount": txn["amount"],
        "accountId": f"acct-{txn['account'].lower().replace(' ', '-')}",
        "payee": txn["api_payee"],
        "coa": {"type": "CATEGORY", "id": txn["category"]},
        "state": "CLEARED",
    }
    record.update(overrides)
    return record


@pytest.fixture
def cross_source(tmp_path: Path) -> tuple[list[dict], list[dict]]:
    """The same four transactions as both adapters produce them."""
    csv_rows = SimplifiCsvSource(write_csv(tmp_path / "export.csv", SHARED_TRANSACTIONS)).fetch()
    api_rows = api_source_serving(
        [api_transaction(txn) for txn in SHARED_TRANSACTIONS],
        [
            {"id": f"acct-{name.lower().replace(' ', '-')}", "name": name}
            for name in {txn["account"] for txn in SHARED_TRANSACTIONS}
        ],
    ).fetch()
    return csv_rows, api_rows


# --- the two adapters agree --------------------------------------------------


def test_both_adapters_produce_the_same_record_shape(cross_source):
    """A field present on one path and absent on the other is a trap.

    Downstream reads rows, not adapters. A consumer that works on CSV rows and
    KeyErrors on API rows — or worse, silently takes a `.get` default — is the
    failure this shared constructor exists to prevent.
    """
    csv_rows, api_rows = cross_source

    assert set(csv_rows[0]) == set(api_rows[0])


@pytest.mark.parametrize("index", range(len(SHARED_TRANSACTIONS)))
def test_equivalent_source_facts_produce_equivalent_evidence(cross_source, index):
    csv_evidence = evidence.evidence_from_row(cross_source[0][index])
    api_evidence = evidence.evidence_from_row(cross_source[1][index])

    assert csv_evidence.merchant.canonical == api_evidence.merchant.canonical
    assert csv_evidence.money == api_evidence.money
    assert csv_evidence.posted_on == api_evidence.posted_on
    assert csv_evidence.account.display == api_evidence.account.display
    assert csv_evidence.kind == api_evidence.kind


def test_a_foreign_charge_collapses_to_one_merchant_across_sources(cross_source):
    """The pre-conversion prefix is stripped for identity on both paths.

    "2.90 Euro Mercadona Calella" and "Mercadona Calella" are one merchant. If
    only one adapter stripped the prefix, the same shop would train two separate
    memories and neither would reach the observation threshold.
    """
    csv_row, api_row = cross_source[0][2], cross_source[1][2]

    assert csv_row["payee_canonical"] == api_row["payee_canonical"]
    # The prefix says what the issuer converted FROM. It is never the row's
    # currency: both rows settle in USD.
    assert api_row["original_currency"] == "EUR"
    assert api_row["currency"] == "USD"
    assert evidence.evidence_from_row(api_row).money.currency == "USD"


def test_deterministic_analysis_evidence_matches_across_sources(cross_source):
    """Equivalent normalized facts, equivalent packet transactions.

    Compared field by field with the source-specific fields removed, because
    those legitimately differ and inventing agreement would be the bug: the CSV
    has no stable transaction ID and no settlement state, the API bulk read does
    not expose the report-exclusion flag, and `reason_codes` is precisely the
    channel that reports those differences.
    """
    csv_rows, api_rows = cross_source
    source_specific = {
        "transaction_id",
        "transaction_state",
        "match_state",
        "provenance",
        "reason_codes",
    }

    def comparable(transaction: dict) -> dict:
        # `foreign_charge` is evidence of what the *descriptor* carried. Only
        # the API sees the descriptor; Simplifi's renamed payee has already lost
        # the "2.90 Euro" prefix by the time it reaches the CSV. Asserting
        # agreement here would demand the CSV adapter invent a fact its source
        # does not contain.
        flags = {k: v for k, v in transaction["flags"].items() if k != "foreign_charge"}
        return {
            **{k: v for k, v in transaction.items() if k not in source_specific},
            "flags": flags,
        }

    for csv_row, api_row in zip(csv_rows, api_rows, strict=True):
        assert comparable(review_packet._transaction(csv_row)) == comparable(
            review_packet._transaction(api_row)
        )


# --- the provider's identifiers stay internal --------------------------------


def test_an_unnamed_account_is_recorded_as_unnamed_not_as_its_id():
    """The fallback this replaces put a provider ID under a display label."""
    rows = api_source_serving(
        [api_transaction(SHARED_TRANSACTIONS[0])],
        [{"id": "acct-everyday-checking"}],  # the provider returned no name
    ).fetch()

    assert rows[0]["account_name"] == ""
    assert rows[0]["account_name_known"] == 0
    assert rows[0]["account_id"] == "acct-everyday-checking"


def test_an_unnamed_account_is_named_explicitly_in_every_artifact():
    rows = api_source_serving(
        [api_transaction(SHARED_TRANSACTIONS[0])],
        [{"id": "acct-everyday-checking"}],
    ).fetch()
    row = rows[0]

    assert evidence.evidence_from_row(row).account.display == evidence.UNKNOWN_ACCOUNT
    assert review_packet._transaction(row)["account_name"] == evidence.UNKNOWN_ACCOUNT
    # A model gets nothing rather than a placeholder: the placeholder means
    # nothing to it, and it would look like one account shared across rows.
    assert egress.sendable_account(row) is None


def test_an_unnamed_account_is_a_diagnostic_not_a_disqualification():
    """The row's own facts are all present; only its label is missing."""
    rows = api_source_serving(
        [api_transaction(SHARED_TRANSACTIONS[0])],
        [{"id": "acct-everyday-checking"}],
    ).fetch()

    assert rows[0]["review_eligible"] == 1
    assert "account_name_unknown" in rows[0]["eligibility_reason_codes"]


def test_two_unnamed_accounts_are_not_pooled_into_one():
    """Their display names collide; their correlation keys must not."""
    first = evidence.AccountRef(name="", provider_id="acct-a")
    second = evidence.AccountRef(name="", provider_id="acct-b")

    assert first.display == second.display == evidence.UNKNOWN_ACCOUNT
    assert first.correlation_key != second.correlation_key


def test_a_legacy_row_carrying_the_old_id_fallback_is_still_read_safely():
    """Rows written before migration 014 can still hold the ID as a name."""
    legacy = {"account_name": "acct-99887766", "account_id": "acct-99887766"}

    ref = evidence.account_ref(legacy)

    assert not ref.is_named
    assert ref.display == evidence.UNKNOWN_ACCOUNT


def test_a_descriptor_the_csv_never_carried_is_a_source_divergence():
    """Not everything the two sources say can be reconciled, and this is one.

    58% of API rows echo the bank descriptor in `payee`. Where that descriptor
    carries a store number and a city the CSV's renamed payee never had, the
    two sources are stating different facts about merchant identity, and the
    normalizer cannot invent the rename. The seam's job is to make that
    divergence visible and safe, not to paper over it — so the canonical keys
    differ, and this test exists to fail loudly if someone later "fixes" it by
    guessing.
    """
    csv_side = evidence.merchant_identity("Costco")
    api_side = evidence.merchant_identity("COSTCO WHSE #1166        NORTH PLAINFINJ")

    assert csv_side.canonical != api_side.canonical


def test_the_descriptor_still_never_reaches_an_agent_facing_path():
    """Whatever identity it yields, the raw string is not display material."""
    row = api_source_serving(
        [api_transaction(SHARED_TRANSACTIONS[0], payee="DEBIT CARD PURCHASE GEICO 800-841-3000")],
        [{"id": "acct-everyday-checking", "name": "Everyday Checking"}],
    ).fetch()[0]

    display = evidence.evidence_from_row(row).merchant.safe_display()

    assert "800-841-3000" not in display
    assert "800-841-3000" not in egress.sendable_payee(row)
    assert "800-841-3000" not in str(review_packet._transaction(row))


def test_a_provider_rename_is_honoured_when_it_really_is_a_rename():
    """Simplifi's `payee` is a rename only when it differs from the descriptor."""
    renamed = evidence.merchant_identity("SQ *AURORA BAKERY 4029", provider_label="Aurora Bakery")
    echoed = evidence.merchant_identity(
        "SQ *AURORA BAKERY 4029", provider_label="SQ *AURORA BAKERY 4029"
    )

    assert renamed.safe_display() == "Aurora Bakery"
    # No rename happened, so the normalizer's stripped output is the name —
    # never the provider's echo of the descriptor.
    assert echoed.safe_display() == "AURORA BAKERY"


def test_a_stored_display_equal_to_the_raw_descriptor_is_not_trusted():
    """Defence for rows written before the adapters agreed on this field."""
    legacy = {
        "payee_raw": "COSTCO WHSE #1166 NORTH PLAINFINJ",
        "payee_display": "COSTCO WHSE #1166 NORTH PLAINFINJ",
        "payee_normalized": "Costco Whse",
        "payee_canonical": "costco_whse",
    }

    assert evidence.evidence_from_row(legacy).merchant.safe_display() == "Costco Whse"


# --- money is the currency's, not the dollar's -------------------------------


def test_a_zero_decimal_currency_is_not_divided_by_a_hundred(tmp_path):
    """¥1,500 is 1500 minor units, not 15.00 of anything."""
    rows = SimplifiCsvSource(
        write_csv(
            tmp_path / "jpy.csv",
            [{**SHARED_TRANSACTIONS[0], "amount": "-1500"}],
        ),
        currency="JPY",
    ).fetch()
    money = evidence.evidence_from_row(rows[0]).money

    assert money.minor_units == -1500
    assert money.currency == "JPY"
    assert money.exponent == 0
    assert money.formatted() == "-1500"


def test_the_same_written_amount_scales_by_the_currency_s_exponent(tmp_path):
    usd = SimplifiCsvSource(
        write_csv(tmp_path / "usd.csv", [{**SHARED_TRANSACTIONS[0], "amount": "-1500"}]),
        currency="USD",
    ).fetch()[0]
    jpy = SimplifiCsvSource(
        write_csv(tmp_path / "jpy.csv", [{**SHARED_TRANSACTIONS[0], "amount": "-1500"}]),
        currency="JPY",
    ).fetch()[0]

    assert usd["amount_minor_units"] == -150_000
    assert jpy["amount_minor_units"] == -1_500


def test_a_zero_decimal_amount_with_a_fraction_is_refused_not_rounded(tmp_path):
    """There is no half yen. Rounding would hide a currency mismatch."""
    with pytest.raises(ValueError, match="sub-minor-unit precision"):
        SimplifiCsvSource(
            write_csv(tmp_path / "bad.csv", [{**SHARED_TRANSACTIONS[0], "amount": "-1500.50"}]),
            currency="JPY",
        ).fetch()


def test_every_artifact_renders_a_zero_decimal_amount_at_its_own_precision(tmp_path):
    rows = SimplifiCsvSource(
        write_csv(tmp_path / "jpy.csv", [{**SHARED_TRANSACTIONS[0], "amount": "-1500"}]),
        currency="JPY",
    ).fetch()
    row = rows[0]

    assert review_packet._transaction(row)["amount"] == {
        "minor_units": -1500,
        "currency": "JPY",
        "currency_exponent": 0,
    }
    # The bare figure is what the payload scan matches on; the model is shown
    # the figure with its currency.
    assert egress.format_amount(row) == "-1500"
    assert egress.sendable_amount(row) == "-1500 JPY"
    # The band edges are minor-unit thresholds, so they read as ¥2000 here and
    # as $20 on a USD row. The label describes the row's own magnitudes.
    assert egress.amount_band(row) == "debit 0-2000 JPY"


def test_a_derived_figure_uses_the_row_s_currency_not_the_row_s_amount():
    """Medians and baselines share the row's currency and nothing else."""
    row = {"amount_minor_units": -1500, "currency": "JPY", "currency_exponent": 0}

    fact = evidence.evidence_from_row(row).amount_evidence(minor_units=42_000)

    assert fact == {
        "minor_units": 42_000,
        "currency": "JPY",
        "currency_exponent": 0,
        "amount": 42_000.0,
    }


def test_a_three_decimal_currency_is_not_treated_as_two(tmp_path):
    """BHD has three places. Two would store 1.234 as unrepresentable and
    1.23 as BHD 0.123 — wrong by a factor of ten, and internally consistent
    the whole way to the report, so nothing downstream could notice."""
    rows = SimplifiCsvSource(
        write_csv(tmp_path / "bhd.csv", [{**SHARED_TRANSACTIONS[0], "amount": "-1.234"}]),
        currency="BHD",
    ).fetch()
    money = evidence.evidence_from_row(rows[0]).money

    assert money.exponent == 3
    assert money.minor_units == -1234
    assert money.formatted() == "-1.234"


def test_the_exponent_table_covers_every_non_default_currency():
    """The default is only safe if the exceptions are complete, not sampled."""
    from simplifi_runtime.money import CURRENCY_EXPONENTS

    zero_decimal = {"JPY", "KRW", "CLP", "ISK", "VND", "XOF", "XAF", "XPF", "RWF", "UGX"}
    three_decimal = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}

    assert all(CURRENCY_EXPONENTS.get(code) == 0 for code in zero_decimal)
    assert all(CURRENCY_EXPONENTS.get(code) == 3 for code in three_decimal)
    assert CURRENCY_EXPONENTS.get("CLF") == 4
    # Everything absent really is exponent 2 by the standard's own default.
    assert all(exponent != 2 for exponent in CURRENCY_EXPONENTS.values())


def test_money_read_back_from_an_unrecognised_currency_stays_readable():
    """Lenient on the way out of the database, strict on the way in.

    A stored row naming a code this table has never heard of must still be
    readable — refusing would make an old row unreadable rather than uncertain.
    """
    assert Money(1234, "XYZ").exponent == 2
    assert Money(1234, "XYZ").formatted() == "12.34"


@pytest.mark.parametrize("bad", ["dollars", "USDD", "US", "", "12A"])
def test_a_currency_that_is_not_a_code_is_refused_at_ingest(tmp_path, bad):
    """Accepting a typo scales the whole dataset by the wrong power of ten."""
    with pytest.raises(ValueError, match="ISO 4217"):
        SimplifiCsvSource(tmp_path / "unused.csv", currency=bad)


# --- the migration cleans up what the old fallback left behind ---------------


def _legacy_store(
    tmp_path: Path,
    rows: list[tuple[str, str, str | None]],
    *,
    eligibility: str = "report_exclusion_unknown,eligible",
    review_eligible: int = 1,
    exclusion_flag: int = 2,
    posted_on: str = "2026-08-01",
):
    """A pre-014 database holding rows the old adapter would have written.

    Migrated only as far as 013, then written to with raw SQL — the current
    store code cannot write a schema that predates its own column list, and a
    test that used it would be testing today's writer rather than yesterday's
    data.
    """
    from simplifi_runtime.store import Store

    source = Store(tmp_path / "probe.sqlite").migrations_dir
    staged = tmp_path / "staged"
    staged.mkdir()
    for sql_file in sorted(Path(source).glob("*.sql")):
        if sql_file.name > "013_retirement_provenance.sql":
            continue
        (staged / sql_file.name).write_text(sql_file.read_text(encoding="utf-8"))

    legacy = Store(tmp_path / "legacy.sqlite", migrations_dir=staged)
    run_id = legacy.start_run("api", "legacy")
    columns = (
        "transaction_id, run_id, observed_at, source_hash, is_current, source,"
        " algorithm_version, ruleset_version, posted_on, account_name, account_id,"
        " amount_minor_units, currency, currency_exponent, payee_raw, payee_normalized,"
        " payee_canonical, payee_display, norm_rules_applied, is_foreign_charge,"
        " category, is_uncategorized, recurring_flag, kind, poisons_statistics,"
        " semantics_reasons, exclusion_flag, eligibility_reason_codes, review_eligible"
    )
    for transaction_id, account_name, account_id in rows:
        legacy.conn.execute(
            f"INSERT INTO transaction_version ({columns}) VALUES ("
            " ?, ?, '2026-08-01T00:00:00+00:00', 'hash', 1, 'api',"
            " '0.1.0', '0.2.0', ?, ?, ?,"
            " -1200, 'USD', 2, 'Costco', 'Costco',"
            " 'costco', 'Costco', '', 0,"
            " 'Groceries', 0, 0, 'spend', 0,"
            " '', ?, ?, ?)",
            (
                transaction_id,
                run_id,
                posted_on,
                account_name,
                account_id,
                exclusion_flag,
                eligibility,
                review_eligible,
            ),
        )
    legacy.conn.commit()
    legacy.conn.close()
    return Store(tmp_path / "legacy.sqlite", migrations_dir=Path(source))


def _stored(store, transaction_id: str) -> dict:
    return dict(
        store.conn.execute(
            "SELECT account_name, account_name_known, account_id,"
            " eligibility_reason_codes, review_eligible"
            " FROM transaction_version WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
    )


def test_migration_014_clears_an_account_id_left_in_the_account_name(tmp_path):
    """The old fallback's value is a provider ID, and it must not survive."""
    store = _legacy_store(tmp_path, [("api-1", "acct-99887766", "acct-99887766")])

    row = _stored(store, "api-1")

    assert row["account_name"] == ""
    assert row["account_name_known"] == 0
    # Still reachable where the allowlist and the packet contract refuse it.
    assert row["account_id"] == "acct-99887766"
    assert "account_name_unknown" in row["eligibility_reason_codes"]
    assert evidence.account_ref(row).display == evidence.UNKNOWN_ACCOUNT


def test_migration_014_restores_review_for_a_row_the_old_rule_disqualified(tmp_path):
    """The old rule required the account name; the new one does not.

    Annotating without recomputing would leave the row `review_eligible = 0`
    and out of every `analyze` until something happened to re-ingest it — a
    current row silently omitted under a policy that says it is reviewable.
    """
    store = _legacy_store(
        tmp_path,
        [("api-4", "", None)],
        eligibility="missing_required_field",
        review_eligible=0,
    )

    row = _stored(store, "api-4")
    codes = row["eligibility_reason_codes"].split(",")

    assert row["review_eligible"] == 1
    assert "missing_required_field" not in codes
    assert "account_name_unknown" in codes
    assert "eligible" in codes


def test_migration_014_keeps_a_row_that_is_genuinely_incomplete_out(tmp_path):
    """Only the account-name half of the verdict is revisited."""
    store = _legacy_store(
        tmp_path,
        [("api-5", "", None)],
        eligibility="missing_required_field",
        review_eligible=0,
        posted_on="",
    )

    row = _stored(store, "api-5")

    assert row["review_eligible"] == 0
    assert "missing_required_field" in row["eligibility_reason_codes"]


def test_migration_014_keeps_a_user_excluded_row_excluded(tmp_path):
    """An unnamed account says nothing about the user's own exclusion flag."""
    store = _legacy_store(
        tmp_path,
        [("api-6", "", None)],
        eligibility="excluded_from_reports,missing_required_field",
        review_eligible=0,
        exclusion_flag=1,
    )

    assert _stored(store, "api-6")["review_eligible"] == 0


def test_staleness_does_not_merge_two_unnamed_accounts():
    """One active unnamed account would hide a stale one completely."""
    from datetime import date

    from simplifi_runtime import prioritize

    def unnamed(account_id: str, posted_on: str) -> dict:
        return {
            "transaction_id": f"{account_id}-{posted_on}",
            "posted_on": posted_on,
            "account_name": "",
            "account_name_known": 0,
            "account_id": account_id,
            "amount_minor_units": -1200,
            "currency": "USD",
            "txn_state": "CLEARED",
        }

    staleness = prioritize.activity_staleness(
        [unnamed("acct-a", "2026-08-01"), unnamed("acct-b", "2026-01-01")],
        today=date(2026, 8, 5),
    )

    assert len(staleness) == 2
    assert {entry["status"] for entry in staleness} == {"ok", "stale"}
    # Both render under the placeholder; neither is silently absorbed by it.
    assert {entry["account"] for entry in staleness} == {evidence.UNKNOWN_ACCOUNT}


def test_a_recurring_finding_states_its_own_currency():
    """These read `$1,000.00 -> $1,200.00` whatever the currency was."""
    from datetime import date

    from simplifi_runtime import subscriptions

    def charge(index: int, minor_units: int) -> dict:
        return {
            "transaction_id": f"jpy-{index}",
            "posted_on": f"2026-{index:02d}-01",
            "payee_canonical": "streamline_video",
            "account_name": "Travel Card",
            "account_name_known": 1,
            "amount_minor_units": -minor_units,
            "currency": "JPY",
            "currency_exponent": 0,
            "category": "Subscriptions",
            "kind": "spend",
            "txn_state": "CLEARED",
            "exclusion_flag": 0,
        }

    # 1,000 -> 2,000 in a zero-decimal currency: the same shape as the USD hike
    # fixtures, with amounts that are minor units rather than cents.
    amounts = [1000, 1000, 1000, 1000, 2000, 2000]
    rows = [charge(index + 1, amount) for index, amount in enumerate(amounts)]
    findings = subscriptions.analyse(rows, today=date(2026, 6, 15))
    hikes = [f for f in findings if f.kind == "hike"]

    assert hikes, "the series should be detected as a price increase"
    assert "JPY" in hikes[0].detail
    assert "$" not in hikes[0].detail


def test_migration_014_leaves_a_real_account_name_alone(tmp_path):
    store = _legacy_store(tmp_path, [("api-2", "Everyday Checking", "acct-99887766")])

    row = _stored(store, "api-2")

    assert row["account_name"] == "Everyday Checking"
    assert row["account_name_known"] == 1
    assert "account_name_unknown" not in row["eligibility_reason_codes"]


def test_migration_014_is_idempotent_against_its_own_diagnostic(tmp_path):
    """Re-running must not stack the reason code twice."""
    store = _legacy_store(tmp_path, [("api-3", "", "acct-1")])
    store.conn.executescript(
        (Path(store.migrations_dir) / "014_account_identity.sql")
        .read_text(encoding="utf-8")
        .replace("ALTER TABLE", "-- ALTER TABLE")
    )

    codes = _stored(store, "api-3")["eligibility_reason_codes"].split(",")

    assert codes.count("account_name_unknown") == 1


def test_the_packet_and_the_report_state_one_annual_impact(tmp_path):
    """Two artifacts formatting the same figure will eventually disagree."""
    from datetime import date

    from simplifi_runtime import report, review_packet, subscriptions

    rows = [
        {
            "transaction_id": f"jpy-{index}",
            "posted_on": f"2026-{index:02d}-01",
            "payee_canonical": "streamline_video",
            "payee_raw": "Streamline Video",
            "payee_normalized": "Streamline Video",
            "payee_display": "Streamline Video",
            "account_name": "Travel Card",
            "account_name_known": 1,
            "amount_minor_units": -amount,
            "currency": "JPY",
            "currency_exponent": 0,
            "category": "Subscriptions",
            "is_uncategorized": 0,
            "kind": "spend",
            "txn_state": "CLEARED",
            "exclusion_flag": 0,
            "review_eligible": 1,
            "eligibility_reason_codes": "eligible",
        }
        for index, amount in enumerate([1000, 1000, 1000, 1000, 2000, 2000], start=1)
    ]
    findings = subscriptions.analyse(rows, today=date(2026, 6, 15))
    assert findings

    packet = review_packet.build_packet(
        run_id=1,
        source="csv",
        analysis_date="2026-06-15",
        rows=rows,
        prioritized=[],
        proposals=[],
        subscription_findings=findings,
    )
    impact = next(
        finding["evidence"]["annual_impact"]
        for finding in packet["findings"]
        if finding["scope"] == "merchant_series"
    )
    rendered = report.render(
        run_id=1,
        source="csv",
        analysis_date="2026-06-15",
        rows=rows,
        prioritized=[],
        staleness=[],
        proposals=[],
        memory_stats={},
        subscription_findings=findings,
    )

    assert impact["currency"] == "JPY"
    assert impact["currency_exponent"] == 0
    # The figure a person reads and the figure the packet states are one value.
    assert (
        review_packet.series_annual_impact(findings[0], rows).minor_units == (impact["minor_units"])
    )
    assert f"{impact['minor_units']:,} JPY" in rendered
