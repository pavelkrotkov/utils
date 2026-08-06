from simplifi_runtime.semantics import (
    Kind,
    classify,
    is_projected,
    is_real_charge,
    is_settled,
)


def test_transfer_detected_from_destination_account_category():
    semantics = classify(
        category="REI Co-op Mastercard",
        payee_raw="CAPITAL ONE CRCARDPMT",
        amount_minor_units=-2500,
        exclusion_flag=True,
        account_names={"REI Co-op Mastercard", "Fidelity CMA"},
    )

    assert semantics.kind is Kind.TRANSFER
    assert semantics.poisons_statistics


def test_paycheck_is_income_not_refund():
    semantics = classify(
        category="Personal Income:Paycheck",
        payee_raw="ACME CORP DIRECT DEP",
        amount_minor_units=500000,
        exclusion_flag=False,
    )

    assert semantics.kind is Kind.INCOME


def test_positive_amount_in_spending_category_is_refund():
    semantics = classify(
        category="Travel",
        payee_raw="Amtrak",
        amount_minor_units=1050,
        exclusion_flag=False,
    )

    assert semantics.kind is Kind.REFUND


def test_ordinary_purchase_is_spend_and_does_not_poison_statistics():
    semantics = classify(
        category="Dining & Drinks:Restaurants",
        payee_raw="Umi Sushi",
        amount_minor_units=-4584,
        exclusion_flag=False,
    )

    assert semantics.kind is Kind.SPEND
    assert not semantics.poisons_statistics


def test_user_exclusion_flag_is_respected():
    semantics = classify(
        category="Groceries",
        payee_raw="Some Store",
        amount_minor_units=-1000,
        exclusion_flag=True,
    )

    assert semantics.poisons_statistics


def test_pending_is_projected_even_when_past_dated_and_has_model_id():
    row = {
        "txn_state": "PENDING",
        "scheduled_model_id": "522552228473503488",
        "posted_on": "2026-07-06",
    }

    assert is_projected(row)
    assert not is_real_charge(row)


def test_cleared_is_real_even_inside_recurring_series():
    assert is_real_charge(
        {"txn_state": "CLEARED", "scheduled_model_id": "522552228473503488"}
    )


def test_past_date_does_not_imply_real_charge():
    assert is_projected(
        {
            "txn_state": "PENDING",
            "scheduled_model_id": "scheduled-1",
            "posted_on": "2020-01-01",
        }
    )


def test_pending_without_schedule_is_real_pending_activity():
    row = {"txn_state": "PENDING", "posted_on": "2026-08-01"}

    assert not is_projected(row)
    assert is_real_charge(row)
    assert not is_settled(row)


def test_missing_state_is_supported_for_csv_rows():
    assert is_settled({"posted_on": "2026-08-01"})
